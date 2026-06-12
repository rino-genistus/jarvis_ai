import datetime
import os.path
from bs4 import BeautifulSoup

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

load_dotenv()

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
        # Get the full event first
        event = self.service.events().get(
            calendarId=calendar_id,
            eventId=event_id
        ).execute()

        # Merge your updates in
        event.update(updates)

        # Send it back
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
        # Step 1 — find the best URL for the query
        search_results = self.tavily_client.search(
            query=query,
            max_results=1,
        )
        best_url = search_results["results"][0]["url"]
        print(f"Reading: {best_url}")

        # Step 2 — extract full content from that page
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

        timezone_offset = data.get("timezone_offset", 0)  # seconds offset from UTC
        hourly = data.get("hourly", [])

        for hour in hourly:
            local_dt = datetime.datetime.utcfromtimestamp(hour["dt"] + timezone_offset)
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

    def get_weather_alerts(self, latitude: float, longitude: float,):
        """
        Get any active severe weather alerts for a location.
        Use this when the user asks about weather warnings, storms,
        advisories, or any severe weather in their area.
        """
        response = requests.get(f"https://api.openweathermap.org/data/3.0/onecall?lat={latitude}&lon={longitude}&units=imperial&appid={os.getenv('OPENWEATHER_API_KEY')}")
        data = response.json()
        alerts = data.get('alerts', [])
        if not alerts:
            return "No active weather alerts for this location"
        else:
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
        if not self.sp:
            return "Spotify is not connected"
        results = self.sp.search(query, limit=1, type='track')
        tracks = results['tracks']['items']
        if not tracks:
            return "no track found"
        track = tracks[0]
        track_name = track['name']
        artist_name = track['artists'][0]['name']

        try:
            self.sp.add_to_queue(track['uri'])
            return f"Added '{track_name}' by {artist_name} to your queue."  # ← return actual track info
        except spotipy.exceptions.SpotifyException as e:
            if "NO_ACTIVE_DEVICE" in str(e):
                return f"Found '{track_name}' by {artist_name} but Spotify has no active device. Open Spotify and start playing something first."
            return f"Spotify error: {str(e)}"

    def create_playlist(self, playlist_name: str, public: bool = True, collaborative: bool = True, description: str = ""):
        """
        Create a playlist with the playlist name provided, public boolean if provided, collaborative if provided and description if provided
        """
        if not self.sp:
            return "Spotify is not connected"
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
        #print(current_user_playlists)
        """results = [
            {
                "name": item['name'],
                "playlist_uri": item['uri']
            }
            for item in current_user_playlists['items']
        ]"""
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
        if not self.sp:
            return "Spotify is not connected"
        self.sp.next_track()

    def pause_song(self):
        """
        Pauses currently playing song
        If user wants to pause, call this method. If user wants to resume playing call this method.
        """
        if not self.sp:
            return "Spotify is not connected"
        self.sp.pause_playback()

    def shuffle(self, shuffle_on: bool = False):
        """
        Turn shuffle on if shuffle is off and turns shuffle off if shuffle is on.
        """
        if not self.sp:
            return "Spotify is not connected"
        self.sp.shuffle(state=shuffle_on)

    def set_volume(self, volume: int = 50):
        """
        Set volume of playback on device. Values between 0 to 100
        """
        if not self.sp:
            return "Spotify is not connected"
        self.sp.volume(volume_percent=volume)
        return f"Playback volume set to {volume}"

class GmailAgent():

    def __init__(self):
        self.service = self._setup()
    def _setup(self):
        return build("gmail", "v1", credentials=get_google_creds())

    def search_email(self, query:str = "", max_results:int = 10):
        """
        Search emails by any criteria. Supports Gmail search syntax like 'from:name@email.com', 'subject:invoice', 'is:unread', 'after:2026/04/01'.
        Use when user references an email by sender, topic, or keyword.
        """
        results = self.service.users().messages().list(q=query, userId='me', labelIds=['INBOX']).execute()
        messages = results.get('messages', [])
        messages_dict = {}
        if not messages:
            return "No Messages found"
        for msg in messages:
            msg_content = self.service.users().messages().get(userId='me', id=msg['id']).execute()
            messages_dict.update({msg['id']: msg_content['snippet']})
        return messages_dict

    def send_email(self, content: str, to: str = "", cc:str = "", bcc:str = "", subject: str = ""):
        '''
        Compose and send a new email. Use when user asks to send, write, or compose an email to someone.
        '''
        message = EmailMessage()
        message.set_content(content)
        message['To'] = to
        message['Subject'] = subject
        message['Cc'] = cc
        message['Bcc'] = bcc

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {"raw": encoded_message}
        results = self.service.users().messages().send(userId='me', body=create_message).execute()
        print(results)

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
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]
                ).execute()
        return detail

    def get_all_labels(self):
        results = self.service.users().labels().list(userId="me").execute()
        labels = results.get('labels', [])
        labels = labels[:-1]
        return [label['name'] for label in labels]

    def reply_to_email(self,email_id:str = "", body:str = ""):
        """
        Reply to an existing email thread. Call search_emails or get_unread_emails first to get the email ID.
        Use when user wants to respond to an email.
        """

        detail = self.service.users().messages().get(
            userId="me",
            id=email_id,
            format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}

        # Extract and decode body
        body = ""
        payload = detail.get("payload", {})

        if "parts" in payload:
            for part in payload["parts"]:
                if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
                    break
                elif part["mimeType"] == "text/html" and "data" in part.get("body", {}):
                    html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
                    body = BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
        elif "data" in payload.get("body", {}):
            raw = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")
            if payload.get("mimeType") == "text/html":
                body = BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)
            else:
                body = raw

        return {
            "id": email_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body[:3000]  # cap so it doesn't blow up context window
        }

    def mark_as_read(email_id:str):
        """
        Mark a specific email as read. Use when user asks to mark an email as read or after reading an email aloud.
        """
        return

    def delete_email(email_id:str):
        """
        Move an email to trash. Use when user asks to delete, remove, or trash an email.
        """
        return

    def get_drafts(max_results: int = 100):
        """
        Fetch saved email drafts. Use when user asks about drafts or wants to send a previously saved draft.
        """
        return

    def get_sent_emails(max_results: int = 100):
        """
        Fetch recently sent emails. Use when user asks what emails they've sent or wants to check sent history.
        """
        return

    def get_sender_profile(self):
        """
        Returns the email address of the currently authenticated Gmail account.
        Call this before sending if the user hasn't specified which account to use, so you can confirm with them.
        """
        results = self.service.users().getProfile(userId='me').execute()
        return results['emailAddress']
