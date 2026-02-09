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

def read_channel_ids(file_path):
    if not os.path.exists(file_path):
        print(f"エラー: {file_path} が見つかりません。")
        sys.exit(1)
    with open(file_path, 'r', encoding='utf-8') as file:
        channel_ids = [line.strip() for line in file if line.strip()]
    unique_ids = list(dict.fromkeys(channel_ids)) # 重複排除（順序維持）
    if not unique_ids:
        print("エラー: チャンネルIDが記載されていません。")
        sys.exit(1)
    return unique_ids

def iso8601_to_duration(iso_duration):
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(iso_duration)
    if not match:
        return "00:00:00"
    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    seconds = int(match.group(3)) if match.group(3) else 0
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def convert_to_japan_time(utc_time_str):
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

def get_uploads_playlist_id(youtube, channel_id):
    try:
        response = youtube.channels().list(
            id=channel_id,
            part='contentDetails'
        ).execute()
        if not response.get('items'):
            return None
        return response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
    except Exception as e:
        print(f"   ?? チャンネル情報取得エラー ({channel_id}): {e}")
        return None

def get_recent_videos(api_key, channel_id):
    """2026年2月9日以降の動画を取得"""
    youtube = build('youtube', 'v3', developerKey=api_key)
    uploads_playlist_id = get_uploads_playlist_id(youtube, channel_id)
    if not uploads_playlist_id:
        print(f"   ?? チャンネルが見つかりません: {channel_id}")
        return []

    # 指定の日付（2026年2月9日 00:00:00 UTC）
    cutoff_date = datetime(2026, 2, 9, 0, 0, 0, tzinfo=timezone.utc)
    video_ids = []
    next_page_token = None
    is_fetching = True

    while is_fetching:
        try:
            pl_request = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part='snippet',
                maxResults=50,
                pageToken=next_page_token
            )
            pl_response = pl_request.execute()
            
            items = pl_response.get('items', [])
            if not items:
                break

            for item in items:
                published_at_str = item['snippet']['publishedAt']
                dt = date_parser.parse(published_at_str)
                # 指定日より前になったら終了
                if dt < cutoff_date:
                    is_fetching = False
                    break
                video_ids.append(item['snippet']['resourceId']['videoId'])
            
            next_page_token = pl_response.get('nextPageToken')
            if not next_page_token:
                break
        except Exception as e:
            print(f"   ?? プレイリスト取得エラー: {e}")
            break

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
        engagement_rate = calc_engagement_rate(video['like_count'], video['comment_count'], video['view_count'])
        # YouTube URLを正しく生成
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
    channel_id_file = 'channel_ID.txt'
    api_key, spreadsheet_id, service_account_key = get_env_vars()
    
    sheet_name = get_current_japan_digit_date()
    
    # 認証と既存シートの確認
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

    channel_ids = read_channel_ids(channel_id_file)
    exec_time_jst = get_current_japan_time()

    print(f"?? YouTubeデータ取得開始 (対象: 2026/02/09 以降)")
    all_video_data = []
    for idx, channel_id in enumerate(channel_ids, 1):
        print(f"   [{idx}/{len(channel_ids)}] Channel ID: {channel_id} 処理中...")
        channel_videos = get_recent_videos(api_key, channel_id)
        print(f"     -> {len(channel_videos)}件 取得完了")
        all_video_data.extend(channel_videos)

    if not all_video_data:
        print("?? 動画が1件も見つかりませんでした（対象日付以降）。")
        return

    # 重複排除とソート
    unique_videos = {v['video_id']: v for v in all_video_data}.values()
    final_list = sorted(unique_videos, key=lambda x: x['view_count'], reverse=True)

    print(f"?? 合計 {len(final_list)} 件の動画を書き込みます...")

    headers, rows = prepare_rows(final_list, exec_time_jst)

    # 履歴用シートの作成とデータの書き込み
    try:
        worksheet = sh.add_worksheet(title=sheet_name, rows=len(rows)+1, cols=len(headers))
        # ヘッダーとデータを結合して一括アップデート
        worksheet.update('A1', [headers] + rows, value_input_option='USER_ENTERED')
        print(f"? 保存完了")
    except Exception as e:
        print(f"エラー: シートへの書き込みに失敗しました: {e}")

if __name__ == "__main__":
    main()