import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dateutil import parser as date_parser

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# ====== 共通設定 ======
MIN_VIEW_COUNT = 10000              # 閾値: 1万再生
MAX_BELOW_THRESHOLD_IN_PAGE = 5     # 1ページ(最大50件)内で1万未満が5本出たら、そのキーワード終了
MAX_RESULTS_PER_KEYWORD = 500       # 各キーワードの最大取得件数


# ====== 環境変数 ======
def get_env_vars():
    """環境変数からAPIキー、認証情報、スプレッドシートIDを取得"""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    service_account_key = os.environ.get("GCP_SERVICE_ACCOUNT_KEY")

    if not api_key:
        print("エラー: 環境変数 'YOUTUBE_API_KEY' が設定されていません。")
        sys.exit(1)
    if not spreadsheet_id:
        print("エラー: 環境変数 'SPREADSHEET_ID' が設定されていません。")
        sys.exit(1)
    if not service_account_key:
        print("エラー: 環境変数 'GCP_SERVICE_ACCOUNT_KEY' が設定されていません。")
        sys.exit(1)

    return api_key, spreadsheet_id, service_account_key


# ====== config 読み込み（keywords のみ使用） ======
def read_config(file_path):
    """
    config.txt から keywords(複数) を読み込む。

    形式例（複数キーワード）:
        keywords = Python, プログラミング, チュートリアル, AI, 機械学習, データ分析

    互換性として:
        keyword = Python チュートリアル
    も1件として扱う。

    ※ start_date は既に使用しない（取得開始日時はコード側で固定）。
    """
    if not os.path.exists(file_path):
        print(f"エラー: {file_path} が見つかりません。")
        sys.exit(1)

    config = {}
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # 空行 & コメント行(#)は無視
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            key, value = map(str.strip, line.split('=', 1))
            config[key.lower()] = value

    # --- keywords の取得 ---
    keywords_str = None
    if 'keywords' in config:
        keywords_str = config['keywords']
    elif 'keyword' in config:
        keywords_str = config['keyword']

    if not keywords_str:
        print("エラー: config.txt に 'keywords' または 'keyword' が指定されていません。")
        sys.exit(1)

    # カンマ区切りで分割し、前後の空白をトリム
    keywords = [kw.strip() for kw in keywords_str.split(',') if kw.strip()]
    if not keywords:
        print("エラー: キーワードの解析結果が空です（書式を確認してください）。")
        sys.exit(1)

    # start_date が書かれていても、現在は使用しない（互換性のため残すのみ）
    return keywords


# ====== 日付・時間関連 ======
def iso8601_to_duration(iso_duration):
    """ISO8601の長さ表記(PTxxHxxMxxS)を hh:mm:ss に変換"""
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(iso_duration)
    if not match:
        return "00:00:00"
    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    seconds = int(match.group(3)) if match.group(3) else 0
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def convert_to_japan_time(utc_time_str):
    """UTCの日時文字列を日本時間(YYYY/MM/DD HH:MM:SS)に変換"""
    utc_dt = date_parser.parse(utc_time_str)
    JST = timezone(timedelta(hours=9))
    japan_dt = utc_dt.astimezone(JST)
    return japan_dt.strftime("%Y/%m/%d %H:%M:%S")


def get_current_japan_time():
    JST = timezone(timedelta(hours=9))
    now_jst = datetime.now(JST)
    return now_jst.strftime("%Y/%m/%d %H:%M:%S")


def get_current_japan_digit_date():
    JST = timezone(timedelta(hours=9))
    now_jst = datetime.now(JST)
    return now_jst.strftime("%Y%m%d")


def get_cutoff_datetime_jst(days_before=11):
    """
    取得開始日時を「実行日の days_before 日前の 0:00 JST」として返す。
    今回は days_before=11（=約10日間の変遷を追うイメージ）。
    """
    JST = timezone(timedelta(hours=9))
    now_jst = datetime.now(JST)
    cutoff_jst = now_jst - timedelta(days=days_before)
    cutoff_jst = cutoff_jst.replace(hour=0, minute=0, second=0, microsecond=0)
    return cutoff_jst


# ====== 指標計算 ======
def calc_engagement_rate(like_count, comment_count, view_count):
    if view_count == 0:
        return 0.0
    return round((like_count + comment_count) / view_count * 100, 2)


# ====== リトライヘルパー (YouTube API) ======
def youtube_with_retry(request, max_retries=5, base_wait=5):
    """
    YouTube API リクエストをリトライ付きで実行する。
    - 500/502/503/504 など一時的エラーはリトライ対象。
    """
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            if status in (500, 502, 503, 504):
                wait = base_wait * (2 ** attempt)
                print(f"   !! YouTube API エラー {status}。{wait}秒待機してリトライします... "
                      f"(試行 {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            # 4xxなどはリトライしても意味が薄いのでそのまま投げる
            raise
        except Exception as e:
            wait = base_wait * (2 ** attempt)
            print(f"   !! YouTube API 一時的エラー ({e})。{wait}秒待機してリトライします... "
                  f"(試行 {attempt + 1}/{max_retries})")
            time.sleep(wait)
    raise RuntimeError("YouTube API のリトライ上限を超えました")


# ====== リトライヘルパー (Google Sheets / gspread) ======
def sheets_with_retry(operation, max_retries=5, base_wait=10):
    """
    Google Sheets / gspread の操作に対してリトライを行うヘルパー。
    - APIError [500], [503] をリトライ対象とする。
    """
    for attempt in range(max_retries):
        try:
            return operation()
        except APIError as e:
            msg = str(e)
            if "[500]" in msg or "[503]" in msg:
                wait = base_wait * (2 ** attempt)  # 10, 20, 40, 80, 160秒…
                print(f"   !! Google Sheets API 一時エラー ({msg})。{wait}秒待機してリトライします... "
                      f"(試行 {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            # その他のエラーはそのまま投げる
            raise
        except Exception as e:
            wait = base_wait * (2 ** attempt)
            print(f"   !! Google Sheets 一時的エラー ({e})。{wait}秒待機してリトライします... "
                  f"(試行 {attempt + 1}/{max_retries})")
            time.sleep(wait)
    raise RuntimeError("Google Sheets API のリトライ上限を超えました")


# ====== YouTube 検索＆取得 ======
def get_recent_videos_by_search(api_key, keyword, cutoff_dt_utc,
                                max_results_per_keyword=MAX_RESULTS_PER_KEYWORD,
                                min_view_count=MIN_VIEW_COUNT,
                                max_below_threshold_in_page=MAX_BELOW_THRESHOLD_IN_PAGE):
    """
    検索ワードで cutoff_dt_utc(UTC) 以降の動画を取得。
    再生回数順(order=viewCount)で最大 max_results_per_keyword 件まで取得する。

    min_view_count を下回る動画が「1ページ(最大50件)の中で
    max_below_threshold_in_page 本以上」出てきたら、そのキーワードの取得を終了する。
    ※閾値未満の動画も、そこまでに取得した分はリストに含める。
    """
    youtube = build('youtube', 'v3', developerKey=api_key)

    # YouTube API 用の RFC3339 形式に変換
    published_after = cutoff_dt_utc.isoformat().replace("+00:00", "Z")

    final_video_data = []
    next_page_token = None
    stop_due_to_threshold = False

    print(f"      - 検索条件: publishedAfter = {published_after}, 閾値 = {min_view_count} 再生")

    while True:
        # すでに上限近くまで取得していたら終了
        if len(final_video_data) >= max_results_per_keyword:
            break
        if stop_due_to_threshold:
            break

        try:
            search_response = youtube_with_retry(
                youtube.search().list(
                    q=keyword,
                    part='id',
                    type='video',
                    maxResults=50,
                    publishedAfter=published_after,
                    order='viewCount',  # 再生回数順
                    pageToken=next_page_token
                )
            )
        except Exception as e:
            print(f"   ?? 検索エラー (keyword={keyword}): {e}")
            break

        items = search_response.get('items', [])
        if not items:
            break

        # このページの videoId 一覧
        video_ids = []
        for item in items:
            vid = item['id'].get('videoId')
            if vid:
                video_ids.append(vid)

        if not video_ids:
            break

        # このページ分の詳細を取得
        try:
            vid_response = youtube_with_retry(
                youtube.videos().list(
                    part='snippet,statistics,contentDetails',
                    id=','.join(video_ids)
                )
            )
        except Exception as e:
            print(f"   ?? 詳細取得エラー: {e}")
            break

        below_threshold_count_in_page = 0

        for item in vid_response.get('items', []):
            snippet = item['snippet']
            statistics = item.get('statistics', {})
            content_details = item['contentDetails']

            view_count = int(statistics.get('viewCount', 0))
            like_count = int(statistics.get('likeCount', 0))
            comment_count = int(statistics.get('commentCount', 0))

            final_video_data.append({
                'title': snippet['title'],
                'channel': snippet['channelTitle'],
                'published_at': snippet['publishedAt'],
                'video_id': item['id'],
                'view_count': view_count,
                'like_count': like_count,
                'comment_count': comment_count,
                'duration': content_details.get('duration', "PT0S")
            })

            # 閾値判定（このページ内でのカウント）
            if view_count < min_view_count:
                below_threshold_count_in_page += 1
                if below_threshold_count_in_page >= max_below_threshold_in_page:
                    stop_due_to_threshold = True
                    break

            # 全体の件数上限
            if len(final_video_data) >= max_results_per_keyword:
                stop_due_to_threshold = True
                break

        if stop_due_to_threshold:
            break

        next_page_token = search_response.get('nextPageToken')
        if not next_page_token:
            break

    print(f"      - キーワード '{keyword}' の検索終了（内部取得件数: {len(final_video_data)}件）")
    return final_video_data


# ====== 日本語判定・備考生成 ======
JP_PATTERN = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]')


def contains_japanese(text):
    return bool(JP_PATTERN.search(text))


def make_note_for_video(video, prev_views_original, exec_time_jst):
    """
    備考列をルールに従って1つだけ付与する。

    優先度:
      1. 動画長さ <= 3分       → "3分未満"
      2. 再生回数 < 1万        → "1万回未満"
      3. タイトル/チャンネルに日本語なし → "対象外"
      4. 前日の投稿           → "最新投稿"
      5. 前日 <1万 & 今日 >=1万 → "1万回越え"
    """
    # 1) 動画長さが 3分以下 → 3分未満
    duration_str = iso8601_to_duration(video['duration'])  # "hh:mm:ss"
    h, m, s = map(int, duration_str.split(':'))
    duration_seconds = h * 3600 + m * 60 + s
    if duration_seconds <= 3 * 60:
        return "3分未満"

    # 2) 再生回数 1万回未満
    if video['view_count'] < 10000:
        return "1万回未満"

    # 3) タイトル・チャンネル名ともに日本語が含まれていない → 対象外
    title_has_jp = contains_japanese(video['title'])
    channel_has_jp = contains_japanese(video['channel'])
    if not (title_has_jp or channel_has_jp):
        return "対象外"

    # 4) 最新投稿（前日の投稿）
    exec_dt = date_parser.parse(exec_time_jst)
    JST = timezone(timedelta(hours=9))
    exec_dt = exec_dt.astimezone(JST)
    day_before = (exec_dt - timedelta(days=1)).date()

    published_jst = date_parser.parse(
        convert_to_japan_time(video['published_at'])
    ).date()

    if published_jst == day_before:
        return "最新投稿"

    # 5) 1万回越え（前日の再生回数 <1万 & 今日 >=1万）
    if prev_views_original is not None:
        if (prev_views_original < 10000) and (video['view_count'] >= 10000):
            return "1万回越え"

    # どれにも該当しない場合は空欄
    return ""


# ====== 前日シートの再生回数取得 ======
def get_prev_day_views_dict(sh, today_sheet_name):
    """
    今日のシート名 (YYYYMMDD) から前日シート名を計算し、
    そのシートの「動画ID」「再生回数」を dict にして返す。

    戻り値: { video_id: view_count_prev, ... }
    """
    try:
        today_dt = datetime.strptime(today_sheet_name, "%Y%m%d")
    except ValueError:
        return {}

    prev_dt = today_dt - timedelta(days=1)
    prev_sheet_name = prev_dt.strftime("%Y%m%d")

    try:
        prev_ws = sh.worksheet(prev_sheet_name)
    except WorksheetNotFound:
        # 前日シートが存在しない場合は空
        print(f"   ?? 前日シート '{prev_sheet_name}' が存在しないため、前日再生回数は 0 として扱います。")
        return {}

    try:
        prev_values = sheets_with_retry(lambda: prev_ws.get_all_values())
    except Exception as e:
        print(f"   ?? 前日シート '{prev_sheet_name}' の読み込みに失敗しました: {e}")
        return {}

    if not prev_values:
        return {}

    headers = prev_values[0]
    try:
        vid_idx = headers.index("動画ID")
        view_idx = headers.index("再生回数")
    except ValueError:
        # 期待するヘッダがない場合
        print(f"   ?? 前日シート '{prev_sheet_name}' に '動画ID' または '再生回数' 列が見つかりません。")
        return {}

    prev_views_dict = {}
    for row in prev_values[1:]:
        if len(row) <= max(vid_idx, view_idx):
            continue
        video_id = row[vid_idx]
        view_str = row[view_idx]
        try:
            view_count = int(view_str)
        except Exception:
            continue
        prev_views_dict[video_id] = view_count

    print(f"   ?? 前日シート '{prev_sheet_name}' から {len(prev_views_dict)} 件の前日再生回数を取得しました。")
    return prev_views_dict


# ====== 行生成 ======
def prepare_rows(video_data, exec_time_jst, prev_views_dict):
    """
    video_data: ユニーク＆ソート済みの動画情報リスト
    prev_views_dict: {video_id: 前日の再生回数}
    """
    headers = [
        "動画URL",
        "動画タイトル",
        "チャンネル名",
        "投稿日時",
        "再生回数",
        "視聴者コメント数",
        "高評価数",
        "動画の長さ",
        "前日の再生回数",
        "再生回数差分",
        "動画ID",
        "エンゲージメント率(%)",
        "備考",
        "ダウンロード実行時間"
    ]

    rows = []
    for video in video_data:
        current_views = video['view_count']
        video_id = video['video_id']

        # 前日の再生回数（なければ 0）
        prev_views_for_diff = prev_views_dict.get(video_id, 0)
        # 備考用には「前日が存在したかどうか」を判別したいので、元の値も渡す
        prev_views_original = prev_views_dict.get(video_id, None)

        diff = current_views - prev_views_for_diff

        engagement_rate = calc_engagement_rate(
            video['like_count'],
            video['comment_count'],
            current_views
        )
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        jst_time = convert_to_japan_time(video['published_at'])

        note = make_note_for_video(video, prev_views_original, exec_time_jst)

        rows.append([
            video_url,                         # 動画URL
            video['title'],                    # 動画タイトル
            video['channel'],                  # チャンネル名
            jst_time,                          # 投稿日時
            current_views,                     # 再生回数
            video['comment_count'],            # 視聴者コメント数
            video['like_count'],               # 高評価数
            iso8601_to_duration(video['duration']),  # 動画の長さ
            prev_views_for_diff,               # 前日の再生回数（なければ0）
            diff,                              # 再生回数差分
            video_id,                          # 動画ID
            engagement_rate,                   # エンゲージメント率(%)
            note,                              # 備考
            exec_time_jst                      # ダウンロード実行時間
        ])

    print(f"?? 出力用の行データを {len(rows)} 行分生成しました。")
    return headers, rows


# ====== main ======
def main():
    config_file = 'config.txt'
    api_key, spreadsheet_id, service_account_key = get_env_vars()

    sheet_name = get_current_japan_digit_date()

    # スプレッドシート認証と既存シート確認
    try:
        credentials_dict = json.loads(service_account_key)
        creds = Credentials.from_service_account_info(credentials_dict, scopes=SCOPES)
        gc = gspread.authorize(creds)

        # スプレッドシートを開く
        sh = sheets_with_retry(lambda: gc.open_by_key(spreadsheet_id))

        # 既存シート名一覧
        existing_sheets = sheets_with_retry(lambda: [ws.title for ws in sh.worksheets()])
        if sheet_name in existing_sheets:
            print(f"?? シート '{sheet_name}' は既に存在するため、処理をスキップします。")
            return
    except Exception as e:
        print(f"エラー: スプレッドシートの確認に失敗しました: {e}")
        sys.exit(1)

    # config.txt から keywords を取得（start_date は使わない）
    keywords = read_config(config_file)

    # 取得開始日時を「実行日の11日前 0:00 JST」に固定
    start_dt_jst = get_cutoff_datetime_jst(days_before=11)
    cutoff_dt_utc = start_dt_jst.astimezone(timezone.utc)

    exec_time_jst = get_current_japan_time()

    print(
        f"?? YouTubeデータ取得開始 "
        f"(キーワード数: {len(keywords)}, "
        f"対象: {start_dt_jst.strftime('%Y/%m/%d %H:%M:%S')} 以降, 日本時間基準, "
        f"閾値目安: {MIN_VIEW_COUNT}再生)"
    )

    all_video_data = []

    # キーワードごとの取得
    for idx, keyword in enumerate(keywords, 1):
        print(f"   [{idx}/{len(keywords)}] キーワード: '{keyword}' 検索開始...")
        videos = get_recent_videos_by_search(
            api_key,
            keyword,
            cutoff_dt_utc,
            max_results_per_keyword=MAX_RESULTS_PER_KEYWORD,
            min_view_count=MIN_VIEW_COUNT,
            max_below_threshold_in_page=MAX_BELOW_THRESHOLD_IN_PAGE
        )
        print(f"   [{idx}/{len(keywords)}] キーワード: '{keyword}' 取得完了: {len(videos)}件")
        all_video_data.extend(videos)
        print(f"   [{idx}/{len(keywords)}] キーワード: '{keyword}' 出力用リストへの追加完了（累計: {len(all_video_data)}件）")

    if not all_video_data:
        print("?? 動画が1件も見つかりませんでした（対象日時以降 & 全検索条件）。")
        return

    # 動画IDで重複削除
    unique_videos_dict = {}
    for v in all_video_data:
        unique_videos_dict[v['video_id']] = v
    unique_videos = list(unique_videos_dict.values())

    # 再生回数降順でソート
    final_list = sorted(unique_videos, key=lambda x: x['view_count'], reverse=True)

    print(f"?? 合計 {len(final_list)} 件のユニークな動画をシートに書き込み対象とします...")

    # 前日シートから「前日の再生回数」を取得
    prev_views_dict = get_prev_day_views_dict(sh, sheet_name)

    headers, rows = prepare_rows(final_list, exec_time_jst, prev_views_dict)

    # シート作成 & 書き込み & 左端へ移動
    try:
        worksheet = sheets_with_retry(
            lambda: sh.add_worksheet(title=sheet_name, rows=len(rows) + 1, cols=len(headers))
        )
        sheets_with_retry(
            lambda: worksheet.update('A1', [headers] + rows, value_input_option='USER_ENTERED')
        )

        # 新しいシートを左端に移動
        def reorder():
            worksheets = sh.worksheets()
            ordered = [worksheet] + [ws for ws in worksheets if ws.id != worksheet.id]
            sh.reorder_worksheets(ordered)

        sheets_with_retry(reorder)

        print("? 全キーワード分のデータ出力が完了しました。")
    except Exception as e:
        print(f"エラー: シートへの書き込みに失敗しました: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
