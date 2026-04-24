import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dateutil import parser as date_parser

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


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


def read_config(file_path):
    """
    config.txt から keywords(複数) と start_date(JST) を読み込む。

    形式例（複数キーワード）:
        keywords = Python, プログラミング, チュートリアル, AI, 機械学習, データ分析
        start_date = 2026/02/09

    互換性として:
        keyword = Python チュートリアル
    も1件として扱う。
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

    # --- start_date の取得 ---
    start_date_str = config.get('start_date')
    if not start_date_str:
        print("エラー: config.txt に 'start_date' が指定されていません。")
        sys.exit(1)

    # start_date を「日本時間」の日時として解釈
    try:
        dt = date_parser.parse(start_date_str)
    except Exception:
        print("エラー: start_date の形式を解釈できません。例: 2026/02/09 や 2026-02-09, 2026/02/09 12:00 など")
        sys.exit(1)

    JST = timezone(timedelta(hours=9))
    if dt.tzinfo is None:
        # タイムゾーン指定なし → JST とみなす
        dt_jst = dt.replace(tzinfo=JST)
    else:
        # 何かしら tz がついていたら JST に変換
        dt_jst = dt.astimezone(JST)

    return keywords, dt_jst


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


def calc_engagement_rate(like_count, comment_count, view_count):
    if view_count == 0:
        return 0.0
    return round((like_count + comment_count) / view_count * 100, 2)


def get_recent_videos_by_search(api_key, keyword, cutoff_dt_utc, max_results_per_keyword=500):
    """
    検索ワードで cutoff_dt_utc(UTC) 以降の動画を取得。
    再生回数順(order=viewCount)で最大 max_results_per_keyword 件まで取得する。
    """
    youtube = build('youtube', 'v3', developerKey=api_key)

    # YouTube API 用の RFC3339 形式に変換
    published_after = cutoff_dt_utc.isoformat().replace("+00:00", "Z")

    video_ids = []
    next_page_token = None

    while True:
        # すでに上限近くまで取得していたら終了
        if len(video_ids) >= max_results_per_keyword:
            break

        try:
            search_response = youtube.search().list(
                q=keyword,
                part='id',
                type='video',
                maxResults=50,
                publishedAfter=published_after,
                order='viewCount',  # 再生回数順
                pageToken=next_page_token
            ).execute()
        except Exception as e:
            print(f"   ?? 検索エラー (keyword={keyword}): {e}")
            break

        items = search_response.get('items', [])
        if not items:
            break

        for item in items:
            vid = item['id'].get('videoId')
            if vid:
                video_ids.append(vid)
                if len(video_ids) >= max_results_per_keyword:
                    break

        next_page_token = search_response.get('nextPageToken')
        if not next_page_token:
            break

    # 上限超過分を念のためカット
    video_ids = video_ids[:max_results_per_keyword]

    if not video_ids:
        return []

    final_video_data = []
    # 50件ずつ詳細を取得
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i:i+50]
        try:
            vid_response = youtube.videos().list(
                part='snippet,statistics,contentDetails',
                id=','.join(batch_ids)
            ).execute()
            for item in vid_response.get('items', []):
                snippet = item['snippet']
                statistics = item.get('statistics', {})
                content_details = item['contentDetails']
                final_video_data.append({
                    'title': snippet['title'],
                    'channel': snippet['channelTitle'],
                    'published_at': snippet['publishedAt'],
                    'video_id': item['id'],
                    'view_count': int(statistics.get('viewCount', 0)),
                    'like_count': int(statistics.get('likeCount', 0)),
                    'comment_count': int(statistics.get('commentCount', 0)),
                    'duration': content_details.get('duration', "PT0S")
                })
        except Exception as e:
            print(f"   ?? 詳細取得エラー: {e}")

    return final_video_data


def prepare_rows(video_data, exec_time_jst):
    headers = [
        "動画タイトル", "チャンネル名", "投稿日時（日本時間）", "動画ID",
        "動画URL", "再生回数", "高評価数", "視聴者コメント数", "動画の長さ",
        "エンゲージメント率(%)", "ダウンロード実行時間（日本時間）"
    ]
    rows = []
    for video in video_data:
        engagement_rate = calc_engagement_rate(
            video['like_count'],
            video['comment_count'],
            video['view_count']
        )
        video_url = f"https://www.youtube.com/watch?v={video['video_id']}"
        jst_time = convert_to_japan_time(video['published_at'])
        rows.append([
            video['title'],
            video['channel'],
            jst_time,
            video['video_id'],
            video_url,
            video['view_count'],
            video['like_count'],
            video['comment_count'],
            iso8601_to_duration(video['duration']),
            engagement_rate,
            exec_time_jst
        ])
    return headers, rows


def main():
    config_file = 'config.txt'
    api_key, spreadsheet_id, service_account_key = get_env_vars()

    sheet_name = get_current_japan_digit_date()

    # スプレッドシート認証と既存シート確認
    try:
        credentials_dict = json.loads(service_account_key)
        creds = Credentials.from_service_account_info(credentials_dict, scopes=SCOPES)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(spreadsheet_id)

        existing_sheets = [ws.title for ws in sh.worksheets()]
        if sheet_name in existing_sheets:
            print(f"?? シート '{sheet_name}' は既に存在するため、処理をスキップします。")
            return
    except Exception as e:
        print(f"エラー: スプレッドシートの認証に失敗しました: {e}")
        sys.exit(1)

    # config.txt から keywords と start_date(JST) を取得
    keywords, start_dt_jst = read_config(config_file)
    cutoff_dt_utc = start_dt_jst.astimezone(timezone.utc)

    exec_time_jst = get_current_japan_time()

    print(
        f"?? YouTubeデータ取得開始 "
        f"(キーワード数: {len(keywords)}, "
        f"対象: {start_dt_jst.strftime('%Y/%m/%d %H:%M:%S')} 以降, 日本時間基準)"
    )

    all_video_data = []
    for idx, keyword in enumerate(keywords, 1):
        print(f"   [{idx}/{len(keywords)}] キーワード: '{keyword}' 処理中...")
        videos = get_recent_videos_by_search(api_key, keyword, cutoff_dt_utc, max_results_per_keyword=500)
        print(f"     -> {len(videos)}件 取得")
        all_video_data.extend(videos)

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

    print(f"?? 合計 {len(final_list)} 件のユニークな動画を書き込みます...")

    headers, rows = prepare_rows(final_list, exec_time_jst)

    # シート作成 & 書き込み
    try:
        worksheet = sh.add_worksheet(title=sheet_name, rows=len(rows) + 1, cols=len(headers))
        worksheet.update('A1', [headers] + rows, value_input_option='USER_ENTERED')
        print("? 保存完了")
    except Exception as e:
        print(f"エラー: シートへの書き込みに失敗しました: {e}")


if __name__ == "__main__":
    main()
