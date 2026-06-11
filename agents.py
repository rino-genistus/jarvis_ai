import datetime
import os.path
import send2trash
import shutil
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tavily import TavilyClient
import os
from dotenv import load_dotenv
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import base64
from email.message import EmailMessage
import subprocess
from AppKit import NSWorkspace
from threading import Thread
import json

load_dotenv()
DIRECTORY_CACHE = os.path.join(os.path.expanduser("~"), "jarvis_ai", "directory_cache.json")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

def get_google_creds():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return creds


#List of all agents provided as tools to Jarvis

class Calendar_Agents():

    def __init__(self):
        self.service = self._setup()
    def _setup(self):
        return build("calendar", "v3", credentials=get_google_creds())
    def create_event(self, title: str, date, time, duration_minutes: int, timezone: str="America/New_York", description: str = "", calendarId: str = "primary", location: str=""):
        if isinstance(date, str):
            date = datetime.date.fromisoformat(date)
        if isinstance(time, str):
            time = datetime.time.fromisoformat(time)
        start_time = datetime.datetime.combine(date, time)
        end_time = start_time + datetime.timedelta(minutes=duration_minutes)
        event = {
            'summary': title,
            'location': location,
            'description': description,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': timezone
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': timezone,
            }
        }

        event_result = self.service.events().insert(calendarId = 'primary', body=event).execute()
        print(f"Event created: {event_result.get('htmlLink')}")
        return event_result
    def get_calendar_events(self, start_date: str, end_date: str):
        """
        Fetch calendar events between two dates.
        start_date and end_date should be ISO format strings e.g. '2026-04-16T00:00:00Z'
        """

        if len(start_date) == 10:  # e.g. '2026-04-16'
            start_date = start_date + 'T00:00:00Z'
        if len(end_date) == 10:
            end_date = end_date + 'T23:59:59Z'

        events_result = self.service.events().list(
            calendarId='primary',
            timeMin=start_date,
            timeMax=end_date,
        ).execute()
        events = events_result.get("items", [])
        return events
    def delete_calendar_event(self, event_id: str):
        try:
            delete_event = self.service.events().delete(
                calendarId='primary',
                eventId=event_id
            ).execute()
            print("Event deleted.")
        except Exception as e:
            delete_event = None
            print(f"Failed to delete: {e}")
        return delete_event
    def update_calendar_event(self, event_id: str, updates: dict, calendar_id: str = "primary"):
        event = self.service.events().get(
            calendarId=calendar_id,
            eventId=event_id
        ).execute()
        event.update(updates)
        updated_event = self.service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event
        ).execute()
        return updated_event

class WebSearchAgents():

    def __init__(self):
        self.tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    def search_web(self, query: str):
        """
        Search the web for current information, news, facts, or anything
        that requires up to date knowledge. Use this for weather, news,
        prices, recent events, or any factual question.
        """
        response = self.tavily_client.search(
            query = query,
            max_results = 5,
            include_answer = True,
            search_depth = 'advanced',
        )
        return response['answer']
    def extract_webpages(self, query: str):
        """
        Use this when the user wants to read a full article, page, or document
        in detail — not just a quick answer. Searches for the best URL then
        extracts the full content.
        """
        search_results = self.tavily_client.search(
            query=query,
            max_results=1,
        )
        best_url = search_results["results"][0]["url"]
        print(f"Reading: {best_url}")

        extracted = self.tavily_client.extract(best_url)
        return extracted["results"][0]["raw_content"]
    def crawl_webpages(self):
        """
        Implement a little later.
        """
        return
    def research(self):
        """
        Implement a little later as well.
        """

class WeatherSearch():
    def get_current_weather(self, latitude: float, longitude: float, exclude: list[str]):
        """
        Get the current weather and forecast for a location using its coordinates.
        Use this for any question about current conditions, temperature, humidity,
        wind, or upcoming forecast. Exclude options: 'current', 'minutely', 'hourly',
        'daily', 'alerts'.
        """
        exclude_str = ",".join(exclude) if exclude else ""
        response = requests.get(f"https://api.openweathermap.org/data/3.0/onecall?lat={latitude}&lon={longitude}&exclude={exclude_str}&units=imperial&appid={os.getenv('OPENWEATHER_API_KEY')}")
        return response.json()
    def get_weather_with_time(self, latitude: float, longitude: float, target_hour: str):
        """
        Get the hourly weather forecast for a specific hour today.
        Use this when the user asks about weather at a specific time,
        e.g. 'what will the weather be at 10pm tonight'.
        target_hour should be in 24-hour format (0-23) in the location's local time.
        """
        params = f"lat={latitude}&lon={longitude}&units=imperial&exclude=current,minutely,daily,alerts&appid={os.getenv('OPENWEATHER_API_KEY')}"
        response = requests.get(f"https://api.openweathermap.org/data/3.0/onecall?{params}")
        data = response.json()

        timezone_offset = data.get("timezone_offset", 0)
        hourly = data.get("hourly", [])

        for hour in hourly:
            local_dt = datetime.datetime.fromtimestamp(
                hour["dt"] + timezone_offset, tz=datetime.timezone.utc
            )
            if local_dt.hour == int(target_hour):
                return hour

        return hourly[0] if hourly else {}
    def get_daily_forecast(self, latitude: float, longitude: float, days: int = 7):
        """
        Get the daily weather forecast for the next N days (max 8).
        Use this when the user asks about weather over multiple days,
        a specific day this week, or a general weekly forecast.
        days should be between 1 and 8.
        """
        params = f"lat={latitude}&lon={longitude}&units=imperial&exclude=current,minutely,hourly,alerts&appid={os.getenv('OPENWEATHER_API_KEY')}"
        response = requests.get(f"https://api.openweathermap.org/data/3.0/onecall?{params}")
        data = response.json()

        daily = data.get("daily", [])
        return daily[:days]

    def get_weather_alerts(self, latitude: float, longitude: float):
        """
        Get any active severe weather alerts for a location.
        Use this when the user asks about weather warnings, storms,
        advisories, or any severe weather in their area.
        """
        response = requests.get(f"https://api.openweathermap.org/data/3.0/onecall?lat={latitude}&lon={longitude}&units=imperial&exclude=current,minutely,hourly,daily&appid={os.getenv('OPENWEATHER_API_KEY')}")
        data = response.json()
        alerts = data.get('alerts', [])
        if not alerts:
            return "No active weather alerts for this location"
        return alerts

class SpotifyAgent():

    def __init__(self):
        self.sp = None
        self._setup()

    def _setup(self):
        try:
            auth_manager = SpotifyOAuth(
                client_id=os.getenv("SPOTIPY_CLIENT_ID"),
                client_secret = os.getenv("SPOTIPY_CLIENT_SECRET"),
                redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
                scope = "user-read-playback-state user-modify-playback-state user-library-read playlist-modify-public playlist-modify-private app-remote-control streaming user-read-recently-played playlist-modify-public playlist-modify-private playlist-read-private playlist-read-collaborative",
                cache_path=".spotify_token",
                open_browser=True,
            )
            token = auth_manager.get_cached_token()
            if not token:
                print("\n⚠️  Spotify not authenticated. Opening browser for login...")
                print("After logging in, paste the redirect URL here.\n")
            self.sp = spotipy.Spotify(auth_manager=auth_manager)
            print(self.sp.current_user())
            print("Spotify Connected")
        except Exception as e:
            print(f"Spotify setup failed: {e}")
            self.sp = None

    def _device_check(self):
        """
        Playback commands silently fail with no active device — detect that up
        front so Jarvis can tell the user instead of going quiet.
        Returns an error string, or None when a device is active.
        """
        if not self.sp:
            return "Spotify is not connected"
        try:
            devices = self.sp.devices().get("devices", [])
        except Exception as e:
            return f"Could not reach Spotify: {e}"
        if not any(d.get("is_active") for d in devices):
            names = ", ".join(d.get("name", "?") for d in devices)
            hint = f" Available devices: {names}." if names else ""
            return ("No active Spotify device — ask the user to open Spotify and "
                    "start playback on a device first." + hint)
        return None

    def get_current_track(self):
        if not self.sp:
            return "Spotify not Connected"
        results = self.sp.current_user_playing_track()
        return results

    def search_song_and_queue(self, query: str = ''):
        """
        Search for a song on Spotify by name or artist and return its URI.
        Always call this first before adding to queue or playlist to get the track URI.
        """
        device_error = self._device_check()
        if device_error:
            return device_error
        results = self.sp.search(query, limit=1, type='track')
        tracks = results['tracks']['items']
        if not tracks:
            return "no track found"
        track = tracks[0]
        track_name = track['name']
        artist_name = track['artists'][0]['name']

        try:
            self.sp.add_to_queue(track['uri'])
            return f"Added '{track_name}' by {artist_name} to your queue."
        except spotipy.exceptions.SpotifyException as e:
            if "NO_ACTIVE_DEVICE" in str(e):
                return f"Found '{track_name}' by {artist_name} but Spotify has no active device. Open Spotify and start playing something first."
            return f"Spotify error: {str(e)}"

    def create_playlist(self, playlist_name: str, public: bool = False, collaborative: bool = False, description: str = ""):
        """
        Create a playlist with the given name. collaborative playlists must be private —
        if collaborative is True, public is forced to False.
        """
        if not self.sp:
            return "Spotify is not connected"
        if collaborative:
            public = False
        user_id = self.sp.current_user()["id"]
        playlist = self.sp.user_playlist_create(
            user=user_id,
            name=playlist_name,
            public=public,
            description=description,
            collaborative=collaborative,
        )
        return {"name": playlist["name"], "id": playlist["id"], "url": playlist["external_urls"]["spotify"]}


    def recently_played(self, limit: int = 25):
        """
        Returns the recently played songs
        """
        if not self.sp:
            return "Spotify is not Connected"
        results = self.sp.current_user_recently_played(limit=limit)
        return [
            {
                "name": item['track']['name'],
                "artist": item['track']['artists'][0]['name'],
                "played_at": item['played_at'],
            }
            for item in results['items']
        ]

    def add_song_to_playlist(self, track_name: str, playlist_name: str, limit: int = 1000):
        """
        Adds song to playlist of user's choice. Retrieves song uri and playlist uri if playlist exists and then adds song to the playlist
        """
        if not self.sp:
            return "Spotify is not connected"
        current_user_playlists = self.sp.current_user_playlists()
        playlist_names = [item['name'] for item in current_user_playlists['items']]
        if playlist_name not in playlist_names:
            return "Playlist does not exist."
        playlist_uri = current_user_playlists['items'][playlist_names.index(playlist_name)]['uri']
        results = self.sp.search(track_name, limit=1, type='track')
        track_uri = results['tracks']['items'][0]['uri']
        self.sp.playlist_add_items(playlist_id=playlist_uri, items=[track_uri])
        return f"Added {track_name} to {playlist_name}"

    def skip_song(self):
        """
        Skips currently playing song
        """
        device_error = self._device_check()
        if device_error:
            return device_error
        self.sp.next_track()
<<<<<<< HEAD
        return "Skipped."
=======
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d

    def pause_song(self):
        """
        Pauses currently playing song
        If user wants to pause, call this method. If user wants to resume playing call this method.
        """
        device_error = self._device_check()
        if device_error:
            return device_error
        playback = self.sp.current_playback()
        if playback and playback.get("is_playing"):
            self.sp.pause_playback()
            return "Paused."
        self.sp.start_playback()
        return "Resumed."

    def shuffle(self, shuffle_on: bool = False):
        """
        Turn shuffle on if shuffle is off and turns shuffle off if shuffle is on.
        """
        device_error = self._device_check()
        if device_error:
            return device_error
        self.sp.shuffle(state=shuffle_on)
        return f"Shuffle {'on' if shuffle_on else 'off'}."

    def set_volume(self, volume: int = 50):
        """
        Set volume of playback on device. Values between 0 to 100
        """
        device_error = self._device_check()
        if device_error:
            return device_error
        self.sp.volume(volume_percent=volume)
        return f"Playback volume set to {volume}"

class GmailAgent():

    def __init__(self):
        self.service = self._setup()
    def _setup(self):
        return build("gmail", "v1", credentials=get_google_creds())
<<<<<<< HEAD
    
    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Pull readable text out of a message payload (plain text preferred over HTML)."""
        if "parts" in payload:
            for part in payload["parts"]:
                if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                    return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
            for part in payload["parts"]:
                if part["mimeType"] == "text/html" and "data" in part.get("body", {}):
                    html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
                    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
                if "parts" in part:  # nested multipart
                    nested = GmailAgent._extract_body(part)
                    if nested:
                        return nested
        elif "data" in payload.get("body", {}):
            raw = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")
            if payload.get("mimeType") == "text/html":
                return BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)
            return raw
        return ""

    def _list_messages(self, label_ids=None, query: str = "", max_results: int = 10):
        """Shared list+metadata fetch used by search/unread/sent/drafts views."""
        results = self.service.users().messages().list(
            userId="me",
            q=query or None,
            labelIds=label_ids or [],
            maxResults=max_results,
        ).execute()
        messages = results.get("messages", [])
        emails = []
        for msg in messages:
            detail = self.service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })
        return emails
=======
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d

    def search_email(self, query:str = "", max_results:int = 10):
        """
        Search emails by any criteria. Supports Gmail search syntax like 'from:name@email.com', 'subject:invoice', 'is:unread', 'after:2026/04/01'.
        Use when user references an email by sender, topic, or keyword.
        """
<<<<<<< HEAD
        emails = self._list_messages(label_ids=["INBOX"], query=query, max_results=max_results)
        return emails or "No Messages found"
=======
        results = self.service.users().messages().list(q=query, userId='me', labelIds=['INBOX']).execute()
        messages = results.get('messages', [])
        messages_dict = {}
        if not messages:
            return "No Messages found"
        for msg in messages:
            msg_content = self.service.users().messages().get(userId='me', id=msg['id']).execute()
            messages_dict.update({msg['id']: msg_content['snippet']})
        return messages_dict
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d

    def send_email(self, content: str, to: str = "", cc:str = "", bcc:str = "", subject: str = ""):
        '''
        Compose and send a new email. Use when user asks to send, write, or compose an email to someone.
        '''
        message = EmailMessage()
        message.set_content(content)
        message['To'] = to
        message['Subject'] = subject
        if cc:
            message['Cc'] = cc
        if bcc:
            message['Bcc'] = bcc

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {"raw": encoded_message}
        results = self.service.users().messages().send(userId='me', body=create_message).execute()
<<<<<<< HEAD
        return f"Email sent to {to} (id {results.get('id')})."
    
=======
        print(results)

>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
    def get_unread_emails(self, max_results: int = 10):
        """
        Fetch unread emails from inbox. Returns sender, subject, date, snippet, and ID for each.
        Use when user asks about new or unread emails.
        """
        results = self.service.users().messages().list(
            userId="me",
            labelIds=['UNREAD'],
            maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return "No unread emails."

        emails = []
        for msg in messages:
            detail = self.service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", "")
            })

        return emails

    def get_email_by_id(self, email_id: str):
        """
        Fetch the full body of a specific email by its ID.
        Always call this after get_unread_emails or search_emails when the user wants to read the actual content.
        """
        detail = self.service.users().messages().get(
            userId="me",
            id=email_id,
            format="full"
        ).execute()
<<<<<<< HEAD
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
=======

        payload = detail.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

        body = ""
        parts = payload.get("parts", [])
        if parts:
            for part in parts:
                if part.get("mimeType") == "text/plain":
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8")
                        break
        else:
            data = payload.get("body", {}).get("data", "")
            if data:
                body = base64.urlsafe_b64decode(data).decode("utf-8")

>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
        return {
            "id": email_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
<<<<<<< HEAD
            "body": self._extract_body(detail.get("payload", {}))[:3000],  # cap so it doesn't blow up context window
=======
            "body": body,
            "snippet": detail.get("snippet", "")
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
        }

    def get_all_labels(self):
        results = self.service.users().labels().list(userId="me").execute()
        labels = results.get('labels', [])
<<<<<<< HEAD
        labels = labels[:-1]
        return [label['name'] for label in labels]

    def reply_to_email(self, email_id: str = "", body: str = ""):
        """
        Reply to an existing email thread with the given body text. Call search_email or
        get_unread_emails first to get the email ID. The reply is threaded correctly
        (Re: subject, same conversation). Use when user wants to respond to an email.
        """
        detail = self.service.users().messages().get(
            userId="me",
            id=email_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID", "References"],
        ).execute()
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}

        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        original_msg_id = headers.get("Message-ID", "")

        message = EmailMessage()
        message.set_content(body)
        message["To"] = headers.get("From", "")
        message["Subject"] = subject
        if original_msg_id:
            message["In-Reply-To"] = original_msg_id
            message["References"] = (headers.get("References", "") + " " + original_msg_id).strip()

        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = self.service.users().messages().send(
            userId="me",
            body={"raw": encoded, "threadId": detail.get("threadId")},
        ).execute()
        return f"Replied to {headers.get('From', 'the sender')} (id {result.get('id')})."

    def mark_as_read(self, email_id: str):
        """
        Mark a specific email as read. Use when user asks to mark an email as read or after reading an email aloud.
        """
        self.service.users().messages().modify(
            userId="me", id=email_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return "Marked as read."

    def trash_email(self, email_id: str):
        """
        Move an email to trash. Use when user asks to delete, remove, or trash an email.
        """
        self.service.users().messages().trash(userId="me", id=email_id).execute()
        return "Moved to trash."

    def untrash_email(self, email_id: str):
        """
        Restore an email from the trash. Use when user asks to undo a delete or recover an email.
        """
        self.service.users().messages().untrash(userId="me", id=email_id).execute()
        return "Restored from trash."

    def get_drafts(self, max_results: int = 10):
        """
        Fetch saved email drafts. Use when user asks about drafts or wants to send a previously saved draft.
        """
        results = self.service.users().drafts().list(
            userId="me", maxResults=max_results
        ).execute()
        drafts = results.get("drafts", [])
        if not drafts:
            return "No drafts."
        out = []
        for draft in drafts:
            detail = self.service.users().drafts().get(
                userId="me", id=draft["id"], format="metadata"
            ).execute()
            headers = {h["name"]: h["value"]
                       for h in detail["message"]["payload"].get("headers", [])}
            out.append({
                "draft_id": draft["id"],
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "snippet": detail["message"].get("snippet", ""),
            })
        return out

    def get_sent_emails(self, max_results: int = 10):
        """
        Fetch recently sent emails. Use when user asks what emails they've sent or wants to check sent history.
        """
        emails = self._list_messages(label_ids=["SENT"], max_results=max_results)
        return emails or "Nothing in sent mail."
    
=======
        return [label['name'] for label in labels]

    def reply_to_email(self,email_id:str = "", body:str = ""):
        """
        Reply to an existing email thread. Call search_emails or get_unread_emails first to get the email ID.
        Use when user wants to respond to an email.
        """
        detail = self.service.users().messages().get(userId='me', id=email_id, format='full').execute()
        headers = {h['name']: h['value'] for h in detail['payload']['headers']}
        reply_to = headers.get("From", "")
        subject = headers.get("Subject", "")
        if not subject.startswith("Re: "):
            subject = "Re: " + subject
        message_id = headers.get("Message-ID", "")
        thread_id = detail['threadId']

        raw_message = (
            f"To: {reply_to}\r\n"
            f"Subject: {subject}\r\n"
            f"In-Reply-To: {message_id}\r\n"
            f"References: {message_id}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}"
        )

        encoded = base64.urlsafe_b64encode(raw_message.encode("utf-8")).decode("utf-8")
        results = self.service.users().messages().send(userId='me', body={'raw': encoded, 'threadId': thread_id}).execute()
        return results

    def mark_as_read(self, email_id:str):
        """
        Mark a specific email as read. Use when user asks to mark an email as read or after reading an email aloud.
        """
        results = self.service.users().messages().modify(userId='me', id=email_id, body={"removeLabelIds":['UNREAD']}).execute()
        return "Email marked as Read"

    def trash_email(self, email_id:str):
        """
        Move an email to trash. Use when user asks to delete, remove, or trash an email.
        """
        results = self.service.users().messages().trash(userId='me', id=email_id).execute()
        return 'Email has been moved to the trash'

    def remove_email_from_trash(self, email_id:str):
        """
        Restore an email from trash. Use when user asks to undelete or recover a trashed email.
        """
        results = self.service.users().messages().untrash(userId='me', id=email_id).execute()
        return 'Email has been removed from the trash'

    def get_drafts(self, max_results: int = 100):
        """
        Fetch saved email drafts. Use when user asks about drafts or wants to send a previously saved draft.
        """
        results = self.service.users().messages().list(
            userId="me",
            labelIds=['DRAFT'],
            maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return "No drafts found."

        emails = []
        for msg in messages:
            detail = self.service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", "")
            })

        return emails

    def get_sent_emails(self, max_results: int = 100):
        """
        Fetch recently sent emails. Use when user asks what emails they've sent or wants to check sent history.
        """
        results = self.service.users().messages().list(
            userId="me",
            labelIds=['SENT'],
            maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return "No sent emails found."

        emails = []
        for msg in messages:
            detail = self.service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date", "To"]
            ).execute()

            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            emails.append({
                "id": msg["id"],
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", "")
            })

        return emails

>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
    def get_sender_profile(self):
        """
        Returns the email address of the currently authenticated Gmail account.
        Call this before sending if the user hasn't specified which account to use, so you can confirm with them.
        """
        results = self.service.users().getProfile(userId='me').execute()
        return results['emailAddress']


class ComputerControlAgent():
    def __init__(self):
        self.all_mac_apps = self.get_mac_apps()
        self.apps_dict = {}
        for app in self.all_mac_apps:
            app_name_only = app.split("/")[-1].split(".")[0].lower()
            self.apps_dict.update({app_name_only: app})
        self.home = os.path.expanduser('~')
        self.directories = {}
        self._index_thread = Thread(target=self._build_index, daemon=True)
        self._index_thread.start()

    def _build_index(self):
        if os.path.exists(DIRECTORY_CACHE):
            print("Loading Directory from Cache")
            with open(DIRECTORY_CACHE, "r") as f:
                self.directories = json.load(f)
            print(f"Loaded {len(self.directories)} folders from cache")
        else:
            print("Building Index directory for the first time")
            self.directories = self._index_directories()
            with open(DIRECTORY_CACHE, "w") as f:
                json.dump(self.directories, f)
            print(f"File Index ready. {len(self.directories)} folders indexed.")

    def _index_directories(self):
        SKIP = {
            ".git", ".venv", "__pycache__", "node_modules",
            ".Trash", "Library", ".cache", ".npm", ".conda"
        }

        directory_map = {}
        for root, dirs, files in os.walk(self.home):
            dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
            for folder in dirs:
                full_path = os.path.join(root, folder)
                folder_key = folder.lower()

                if folder_key in directory_map:
                    if isinstance(directory_map[folder_key], list):
                        directory_map[folder_key].append(full_path)
                    else:
                        directory_map[folder_key] = [directory_map[folder_key], full_path]
                else:
                    directory_map[folder_key] = full_path
        return directory_map

    def refresh_index(self):
        """Delete cache and rebuild from scratch."""
        if os.path.exists(DIRECTORY_CACHE):
            os.remove(DIRECTORY_CACHE)
        self.directories = self._index_directories()
        with open(DIRECTORY_CACHE, "w") as f:
            json.dump(self.directories, f)
        print(f"Index rebuilt. {len(self.directories)} folders indexed.")

    def get_mac_apps(self):
        cmd = ['mdfind', "kMDItemKind == 'Application'"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        apps = result.stdout.splitlines()
        return sorted(list(set(apps)))

    def open_application(self, app_name:str):
        if app_name.lower() not in self.apps_dict.keys():
            return "App not found"
        subprocess.run(['open', '-a', self.apps_dict.get(app_name.lower())])
        return f"{app_name} opened"

    def close_application(self, app_name:str):
        """
        Quit an open application by name.
        Use when the user asks to close, quit, or exit an app.
        """
        if app_name.lower() not in self.apps_dict.keys():
            return "App not found"
        script = f'tell application "{app_name}" to quit'
        subprocess.call(['osascript', '-e', script])
        return f"{app_name} closed"

    def switch_application(self, app_name:str):
        script = f'tell application "{app_name}" to activate'
        subprocess.run(['osascript', '-e', script])
        return f"{app_name} brought to the front"

    def list_open_applications(self):
        running_apps = NSWorkspace.sharedWorkspace().runningApplications()
        return [
            {"name": str(app.localizedName()), "bundle_id": str(app.bundleIdentifier())}
            for app in running_apps
            if app.localizedName()
        ]

    def open_file(self, file_path:str):
        try:
            file_path = os.path.expanduser(file_path)
            file_path = os.path.abspath(file_path)
            if not os.path.exists(file_path):
                return f"File not found: {file_path}"
            subprocess.Popen(['open', file_path])
            return f"Opened {file_path}"
        except Exception as e:
            return f"Failed to open file: {e}"

    def create_file(self, file_name:str, content:str = "", location: str = 'desktop'):
        """
        Create a new file with optional content.
        location can be a common directory name like 'desktop', 'documents', 'downloads',
        or a specific folder name that exists on the system, or a full path.
        """
        if not self.directories:
            self._index_thread.join()
        location_key = location.lower()
        if location_key in self.directories:
            directory = self.directories[location_key]
            if isinstance(directory, list):
                return f"Multiple folders name '{location}' found: \n" + "\n".join(directory) + "\nWhich one did you mean? Provide the full path."
        elif os.path.isabs(location):
            directory = location
        else:
            matches = [v for k, v in self.directories.items() if location_key in k]
            if matches:
                directory = matches[0] if not isinstance(matches[0], list) else matches[0][0]
            else:
                return f"Could not find a directory matching '{location}'. Try providing a full path."
        directory = os.path.expanduser(directory)
        directory = os.path.abspath(directory)
        if not os.path.exists(directory):
            return f"Directory does not exist: {directory}"
        file_path = os.path.join(directory, file_name)
        if os.path.exists(file_path):
            return f"File already exists at {file_path}. Choose a different name or location."
        try:
            with open(file_path, "w") as f:
                f.write(content)
            return f"Created {file_path}"
        except PermissionError:
            return f"Permission denied: cannot write to {directory}"
        except Exception as e:
            return f"Failed to create file: {e}"

    def delete_file(self, file_name:str, location:str):
        """
        Move a file to the trash.
        location can be a directory name like 'desktop', 'documents', or a full path.
        """
        if not self.directories:
            self._index_thread.join()
        location_key = location.lower()
        if location_key in self.directories:
            directory = self.directories[location_key]
            if isinstance(directory, list):
                return (
                    f"Multiple folders named '{location}' found:\n" +
                    "\n".join(f"{i+1}. {p}" for i, p in enumerate(directory)) +
                    "\nWhich one did you mean? Provide the full path."
                )
        elif os.path.isabs(location):
            directory = location
        else:
            matches = [v for k, v in self.directories.items() if location_key in k]
            if matches:
                directory = matches[0] if not isinstance(matches[0], list) else matches[0][0]
            else:
                return f"Could not find a directory matching '{location}'. Try providing a full path."
        directory = os.path.expanduser(directory)
        directory = os.path.abspath(directory)
        if not os.path.exists(directory):
            return f"Directory does not exist: {directory}"
        file_path = os.path.join(directory, file_name)
        if not os.path.exists(file_path):
            return f"File not found: {file_path}"
        try:
            send2trash.send2trash(file_path)
            return f"Moved to trash: {file_path}"
        except Exception as e:
            return f"Failed to delete file: {e}"

    def move_file(self, source: str, destination: str):
        """
        Move a file from its current location to a destination directory.
        source should be the full path to the file.
        destination can be a directory name like 'desktop', 'documents', or a full path.
        """
        if not self.directories:
            self._index_thread.join()
        source = os.path.expanduser(source)
        source = os.path.abspath(source)
        if not os.path.exists(source):
            return f"Source file not found: {source}"

        destination_key = destination.lower()
        if destination_key in self.directories:
            dest_dir = self.directories[destination_key]
            if isinstance(dest_dir, list):
                return (
                    f"Multiple folders named '{destination}' found:\n" +
                    "\n".join(f"{i+1}. {p}" for i, p in enumerate(dest_dir)) +
                    "\nWhich one did you mean? Provide the full path."
                )
        elif os.path.isabs(destination):
            dest_dir = destination
        else:
            matches = [v for k, v in self.directories.items() if destination_key in k]
            if matches:
                dest_dir = matches[0] if not isinstance(matches[0], list) else matches[0][0]
            else:
                return f"Could not find a directory matching '{destination}'. Try providing a full path."

        dest_dir = os.path.expanduser(dest_dir)
        dest_dir = os.path.abspath(dest_dir)
        if not os.path.exists(dest_dir):
            return f"Destination directory does not exist: {dest_dir}"

        try:
            dest_path = shutil.move(source, dest_dir)
            return f"Moved to {dest_path}"
        except Exception as e:
            return f"Failed to move file: {e}"
