import datetime
import os.path
from bs4 import BeautifulSoup

from google.auth.exceptions import RefreshError
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
import json
import subprocess
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
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError as e:
                # Google revokes refresh tokens after 7 days while the app is in
                # Testing status, so fall through to a fresh browser login.
                print(f"Google token could not be refreshed ({e}). Re-authenticating...")
        if not refreshed:
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
        """
        Create a new event on the user's calendar.
        date should be 'YYYY-MM-DD' and time should be 'HH:MM' in 24-hour format.
        duration_minutes is how long the event runs.
        Use when the user asks to schedule, book, add or block out something.
        """
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

        try:
            event_result = self.service.events().insert(calendarId='primary', body=event).execute()
        except HttpError as e:
            return f"Could not create the event: {e}"

        print(f"Event created: {event_result.get('htmlLink')}")
        return {
            "id": event_result.get("id"),
            "title": event_result.get("summary"),
            "start": event_result.get("start", {}).get("dateTime"),
            "end": event_result.get("end", {}).get("dateTime"),
            "location": event_result.get("location", ""),
        }
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
            singleEvents=True,   # expand recurring events into their actual instances
            orderBy='startTime',
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return "No events scheduled in that range."

        # Trim to what's worth speaking aloud — the raw API objects are enormous
        return [
            {
                "id": event.get("id"),
                "title": event.get("summary", "(no title)"),
                "start": event.get("start", {}).get("dateTime") or event.get("start", {}).get("date"),
                "end": event.get("end", {}).get("dateTime") or event.get("end", {}).get("date"),
                "location": event.get("location", ""),
            }
            for event in events
        ]
    def delete_calendar_event(self, event_id: str):
        """
        Delete an event from the calendar by its event ID.
        Call get_calendar_events first to find the ID.
        Use when the user asks to cancel, remove or delete something from their schedule.
        """
        try:
            self.service.events().delete(
                calendarId='primary',
                eventId=event_id
            ).execute()
            print("Event deleted.")
        except Exception as e:
            print(f"Failed to delete: {e}")
            return f"Could not delete that event: {e}"
        return "Event deleted."
    def update_calendar_event(self, event_id: str, updates: dict, calendar_id: str = "primary"):
        """
        Change an existing calendar event. Call get_calendar_events first to get the event ID.
        updates is a dictionary of the fields to change, e.g.
        {'summary': 'New title'} to rename, {'location': 'Room 2'} to move it.
        Use when the user asks to reschedule, rename or edit something already on their calendar.
        """
        # Get the full event first
        try:
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
        except HttpError as e:
            return f"Could not update that event: {e}"

        return {
            "id": updated_event.get("id"),
            "title": updated_event.get("summary"),
            "start": updated_event.get("start", {}).get("dateTime"),
            "end": updated_event.get("end", {}).get("dateTime"),
            "location": updated_event.get("location", ""),
        }

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
    def crawl_webpages(self, url: str, instructions: str = "", limit: int = 15):
        """
        Crawl a website and read multiple pages under it, following links from a starting URL.
        Use this when the user wants everything a site says about something rather than one page,
        e.g. 'go through the docs site and tell me how their pricing tiers work'.
        instructions narrows what to look for while crawling.
        """
        try:
            response = self.tavily_client.crawl(
                url=url,
                instructions=instructions or None,
                limit=limit,
                max_depth=2,
                extract_depth="basic",
                format="text",
            )
        except Exception as e:
            return f"Could not crawl that site: {e}"

        pages = response.get("results", [])
        if not pages:
            return f"Nothing readable found at {url}."

        # Cap per page and overall so a big crawl can't blow up the context window
        summaries = []
        for page in pages:
            content = (page.get("raw_content") or "").strip()
            if content:
                summaries.append(f"--- {page.get('url', '')} ---\n{content[:1500]}")
        return "\n\n".join(summaries)[:12000]

    def research(self, query: str):
        """
        Do deep multi-source research on a topic and return a cited synthesis.
        This is slower than search_web — use it only when the user explicitly wants a thorough
        answer, a comparison, or a briefing, not for quick factual lookups.
        """
        try:
            response = self.tavily_client.research(input=query, model="mini")
            answer = response.get("answer") or response.get("output") or ""
            if answer:
                return answer[:8000]
        except Exception as e:
            print(f"Research endpoint unavailable, falling back to search: {e}")

        # Fallback — advanced Q&A search still gives a synthesised answer
        try:
            return self.tavily_client.qna_search(query=query, search_depth="advanced")
        except Exception as e:
            return f"Could not complete the research: {e}"

class WeatherSearch():
    def get_current_weather(self, latitude: float, longitude: float, exclude: list[str] = None):
        """
        Get the current weather for a location using its coordinates.
        Use this for any question about current conditions, temperature, humidity or wind.
        For multi-day questions use get_daily_forecast instead.
        """
        # Default trims the noisy blocks — minutely alone is 60 entries of rainfall
        if exclude is None:
            exclude = ["minutely", "hourly", "daily"]
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

    def _playback_error(self, e):
        """
        Internal helper. Turns a Spotify playback exception into something worth speaking aloud.
        The no-active-device case is by far the most common and has a clear fix.
        """
        if "NO_ACTIVE_DEVICE" in str(e):
            return "Spotify has no active device. Open Spotify and start playing something first."
        return f"Spotify error: {e}"

    def get_current_track(self):
        """
        Get the song playing on Spotify right now, with its artist, album and how far into it we are.
        Use when the user asks what's playing, what this song is, or who sings it.
        """
        if not self.sp:
            return "Spotify not Connected"
        results = self.sp.current_user_playing_track()
        if not results or not results.get("item"):
            return "Nothing is playing on Spotify right now."

        track = results["item"]
        progress_ms = results.get("progress_ms") or 0
        return {
            "name": track["name"],
            "artist": ", ".join(artist["name"] for artist in track["artists"]),
            "album": track["album"]["name"],
            "is_playing": results.get("is_playing", False),
            "progress": f"{progress_ms // 60000}:{(progress_ms // 1000) % 60:02d}",
            "duration": f"{track['duration_ms'] // 60000}:{(track['duration_ms'] // 1000) % 60:02d}",
        }

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

    def create_playlist(self, playlist_name: str, public: bool = True, collaborative: bool = False, description: str = ""):
        """
        Create a new empty playlist on the user's account.
        Set collaborative to true if other people should be able to add songs to it.
        Use when the user asks to make or start a new playlist.
        """
        if not self.sp:
            return "Spotify is not connected"
        # Spotify rejects collaborative playlists that are also public
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

        # Page through the library — current_user_playlists only returns 50 at a time
        playlist_uri = None
        offset = 0
        while offset < limit:
            page = self.sp.current_user_playlists(limit=50, offset=offset)
            items = page.get('items', [])
            if not items:
                break
            for item in items:
                if item['name'].lower() == playlist_name.lower():
                    playlist_uri = item['uri']
                    break
            if playlist_uri:
                break
            offset += 50

        if not playlist_uri:
            return f"No playlist called '{playlist_name}' on your account."

        results = self.sp.search(track_name, limit=1, type='track')
        tracks = results['tracks']['items']
        if not tracks:
            return f"Couldn't find a track called '{track_name}'."

        track = tracks[0]
        self.sp.playlist_add_items(playlist_id=playlist_uri, items=[track['uri']])
        return f"Added '{track['name']}' by {track['artists'][0]['name']} to {playlist_name}."

    def skip_song(self):
        """
        Skip to the next song in the queue.
        Use when the user asks to skip, go to the next track, or says they don't like this song.
        """
        if not self.sp:
            return "Spotify is not connected"
        try:
            self.sp.next_track()
        except spotipy.exceptions.SpotifyException as e:
            return self._playback_error(e)
        return "Skipped to the next track."

    def previous_song(self):
        """
        Go back to the previous song.
        Use when the user asks to go back, replay the last song, or says they skipped too far.
        """
        if not self.sp:
            return "Spotify is not connected"
        try:
            self.sp.previous_track()
        except spotipy.exceptions.SpotifyException as e:
            return self._playback_error(e)
        return "Went back to the previous track."

    def pause_song(self):
        """
        Pause whatever is currently playing.
        Use only when the user wants the music stopped — call resume_song to start it again.
        """
        if not self.sp:
            return "Spotify is not connected"
        try:
            self.sp.pause_playback()
        except spotipy.exceptions.SpotifyException as e:
            return self._playback_error(e)
        return "Playback paused."

    def resume_song(self):
        """
        Resume playback of whatever was paused.
        Use when the user asks to resume, unpause, continue, or start the music again.
        """
        if not self.sp:
            return "Spotify is not connected"
        try:
            self.sp.start_playback()
        except spotipy.exceptions.SpotifyException as e:
            return self._playback_error(e)
        return "Playback resumed."

    def shuffle(self, shuffle_on: bool = False):
        """
        Turn shuffle on or off. Pass shuffle_on as true to turn it on, false to turn it off.
        """
        if not self.sp:
            return "Spotify is not connected"
        try:
            self.sp.shuffle(state=shuffle_on)
        except spotipy.exceptions.SpotifyException as e:
            return self._playback_error(e)
        return f"Shuffle turned {'on' if shuffle_on else 'off'}."

    def set_volume(self, volume: int = 50):
        """
        Set volume of playback on device. Values between 0 to 100
        """
        if not self.sp:
            return "Spotify is not connected"
        volume = max(0, min(100, volume))
        try:
            self.sp.volume(volume_percent=volume)
        except spotipy.exceptions.SpotifyException as e:
            return self._playback_error(e)
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
        results = self.service.users().messages().list(
            userId='me',
            q=query,
            maxResults=max_results
        ).execute()
        messages = results.get('messages', [])
        if not messages:
            return "No emails matched that search."

        emails = []
        for msg in messages:
            detail = self.service.users().messages().get(
                userId='me',
                id=msg['id'],
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date']
            ).execute()
            headers = {h['name']: h['value'] for h in detail['payload']['headers']}
            emails.append({
                "id": msg['id'],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", "")
            })
        return emails

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
        try:
            results = self.service.users().messages().send(userId='me', body=create_message).execute()
        except HttpError as e:
            return f"Failed to send the email: {e}"
        return f"Email sent to {to} with subject '{subject}'."

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

    def _extract_body(self, payload):
        """
        Internal helper. Walks a Gmail payload tree and pulls out readable text,
        preferring text/plain and falling back to stripped HTML.
        """
        if not payload:
            return ""

        mime = payload.get("mimeType", "")
        data = payload.get("body", {}).get("data")

        if data:
            raw = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if mime == "text/html":
                return BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)
            return raw

        # Multipart — recurse, preferring plain text over html
        plain, html = "", ""
        for part in payload.get("parts", []):
            found = self._extract_body(part)
            if not found:
                continue
            if part.get("mimeType") == "text/plain" and not plain:
                plain = found
            elif not html:
                html = found
        return plain or html

    def get_email_by_id(self, email_id: str):
        """
        Fetch the full body of a specific email by its ID.
        Always call this after get_unread_emails or search_email when the user wants to read the actual content.
        """
        try:
            detail = self.service.users().messages().get(
                userId="me",
                id=email_id,
                format="full"
            ).execute()
        except HttpError as e:
            return f"Could not open that email: {e}"

        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        body = self._extract_body(detail.get("payload", {}))

        return {
            "id": email_id,
            "thread_id": detail.get("threadId", ""),
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body[:3000]  # cap so it doesn't blow up the context window
        }

    def get_all_labels(self):
        """
        List every Gmail label on the account, including system labels like INBOX and SENT
        and any custom labels the user has made.
        Use when the user asks what folders or labels they have.
        """
        results = self.service.users().labels().list(userId="me").execute()
        labels = results.get('labels', [])
        return [label['name'] for label in labels]

    def reply_to_email(self, email_id: str = "", body: str = ""):
        """
        Send a reply on an existing email thread. Call get_unread_emails or search_email
        first to get the email ID. body is the text of the reply to send.
        Use when the user wants to respond to an email.
        """
        if not body.strip():
            return "No reply text was given, so nothing was sent."

        try:
            original = self.service.users().messages().get(
                userId="me",
                id=email_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Message-ID", "References", "Reply-To"]
            ).execute()
        except HttpError as e:
            return f"Could not find the email to reply to: {e}"

        headers = {h["name"]: h["value"] for h in original["payload"]["headers"]}
        thread_id = original.get("threadId")

        recipient = headers.get("Reply-To") or headers.get("From", "")
        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        message = EmailMessage()
        message.set_content(body)
        message["To"] = recipient
        message["Subject"] = subject

        # Threading headers — without these Gmail files the reply as a new conversation
        message_id = headers.get("Message-ID")
        if message_id:
            message["In-Reply-To"] = message_id
            references = headers.get("References", "")
            message["References"] = f"{references} {message_id}".strip()

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        try:
            self.service.users().messages().send(
                userId="me",
                body={"raw": encoded_message, "threadId": thread_id}
            ).execute()
        except HttpError as e:
            return f"Failed to send the reply: {e}"

        return f"Replied to {recipient} on '{subject}'."

    def mark_as_read(self, email_id: str):
        """
        Mark a specific email as read by removing its UNREAD label.
        Use when the user asks to mark an email as read, or after reading one aloud to them.
        """
        try:
            self.service.users().messages().modify(
                userId="me",
                id=email_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except HttpError as e:
            return f"Could not mark that email as read: {e}"
        return "Marked as read."

    def mark_as_unread(self, email_id: str):
        """
        Mark a specific email as unread by adding the UNREAD label back.
        Use when the user wants to flag an email to come back to later.
        """
        try:
            self.service.users().messages().modify(
                userId="me",
                id=email_id,
                body={"addLabelIds": ["UNREAD"]}
            ).execute()
        except HttpError as e:
            return f"Could not mark that email as unread: {e}"
        return "Marked as unread."

    def trash_email(self, email_id: str):
        """
        Move an email to the trash. This is reversible — use remove_email_from_trash to undo it.
        Use when the user asks to delete, remove, or trash an email.
        """
        try:
            self.service.users().messages().trash(userId="me", id=email_id).execute()
        except HttpError as e:
            return f"Could not trash that email: {e}"
        return "Email moved to trash."

    def remove_email_from_trash(self, email_id: str):
        """
        Restore an email that was moved to the trash back to the inbox.
        Use when the user says they deleted something by mistake and want it back.
        """
        try:
            self.service.users().messages().untrash(userId="me", id=email_id).execute()
        except HttpError as e:
            return f"Could not restore that email: {e}"
        return "Email restored from trash."

    def get_drafts(self, max_results: int = 20):
        """
        Fetch saved email drafts with their recipient, subject and a snippet of the text.
        Use when the user asks about drafts or what they have waiting to send.
        """
        results = self.service.users().drafts().list(
            userId="me",
            maxResults=max_results
        ).execute()
        drafts = results.get("drafts", [])
        if not drafts:
            return "No saved drafts."

        out = []
        for draft in drafts:
            detail = self.service.users().drafts().get(
                userId="me",
                id=draft["id"],
                format="metadata"
            ).execute()
            message = detail.get("message", {})
            headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
            out.append({
                "draft_id": draft["id"],
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "snippet": message.get("snippet", "")
            })
        return out

    def send_draft(self, draft_id: str):
        """
        Send a draft that is already saved, by its draft ID. Call get_drafts first to get the ID.
        Use when the user asks to send a draft they wrote earlier.
        """
        try:
            self.service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        except HttpError as e:
            return f"Could not send that draft: {e}"
        return "Draft sent."

    def get_sent_emails(self, max_results: int = 10):
        """
        Fetch recently sent emails with recipient, subject, date and a snippet.
        Use when the user asks what they have sent or wants to check their sent history.
        """
        results = self.service.users().messages().list(
            userId="me",
            labelIds=["SENT"],
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
                metadataHeaders=["To", "Subject", "Date"]
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

    def get_sender_profile(self):
        """
        Returns the email address of the currently authenticated Gmail account.
        Call this before sending if the user hasn't specified which account to use, so you can confirm with them.
        """
        results = self.service.users().getProfile(userId='me').execute()
        return results['emailAddress']


class RemindersAgent():
    """
    Talks to the macOS Reminders app so anything Jarvis captures syncs to the
    user's iPhone, Watch and HomePod and actually fires a notification when due.

    Everything goes through JXA (JavaScript for Automation) rather than raw
    AppleScript: user text is passed in as a JSON argv element and never
    concatenated into the script body, so a reminder called 'buy "milk"' can't
    break or inject anything.
    """

    # Reminders stores priority as a number; these are the only values it uses
    PRIORITY_MAP = {"none": 0, "high": 1, "medium": 5, "low": 9}
    PRIORITY_NAMES = {0: "none", 1: "high", 5: "medium", 9: "low"}

    def __init__(self, default_list: str = None):
        self.default_list = default_list

    # ---------------------------------------------------------------- internals

    def _run_jxa(self, script: str, params: dict):
        """
        Internal helper. Runs a JXA script with params handed over as JSON argv
        and decodes the JSON it prints back.
        """
        try:
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", script, json.dumps(params)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"error": "The Reminders app took too long to respond. It may be launching — try again in a moment."}

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # -1743 is the TCC code for "user has not granted Automation access"
            if "-1743" in stderr or "Not authorized" in stderr:
                return {"error": "I don't have permission to control Reminders. Grant it under System Settings, Privacy and Security, Automation."}
            return {"error": f"Reminders returned an error: {stderr}"}

        output = result.stdout.strip()
        if not output:
            return {"error": "Reminders returned nothing."}
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"error": f"Could not read the response from Reminders: {output[:200]}"}

    def _parse_due(self, due: str):
        """
        Internal helper. Turns an ISO-ish date string into the date parts JXA needs.
        Accepts '2026-08-29', '2026-08-29 14:00' or '2026-08-29T14:00:00'.
        A date with no time is treated as 9am so the reminder still fires.
        """
        if not due:
            return None
        cleaned = due.strip().replace("/", "-")
        date_only = len(cleaned) == 10
        try:
            parsed = datetime.datetime.fromisoformat(cleaned.replace(" ", "T"))
        except ValueError:
            return None
        if date_only:
            parsed = parsed.replace(hour=9, minute=0)
        return {
            "year": parsed.year,
            "month": parsed.month,
            "day": parsed.day,
            "hour": parsed.hour,
            "minute": parsed.minute,
        }

    def _to_local(self, iso: str):
        """
        Internal helper. JXA hands back UTC via toISOString(), so convert to the
        user's local clock and drop the tzinfo — otherwise a 2:30pm reminder reads
        back as 6:30pm, and naive/aware comparisons blow up.
        """
        if not iso:
            return None
        try:
            when = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        return when

    def _speak_due(self, iso: str):
        """
        Internal helper. Rewrites an ISO due date as something worth saying out loud.
        """
        if not iso:
            return ""
        when = self._to_local(iso)
        if when is None:
            return iso

        today = datetime.date.today()
        delta = (when.date() - today).days
        clock = when.strftime("%-I:%M %p").lower()

        if delta == 0:
            return f"today at {clock}"
        if delta == 1:
            return f"tomorrow at {clock}"
        if delta == -1:
            return f"yesterday at {clock}"
        if 0 < delta < 7:
            return f"{when.strftime('%A')} at {clock}"
        if delta < 0:
            return f"{when.strftime('%B %-d')} at {clock} (overdue)"
        return f"{when.strftime('%B %-d')} at {clock}"

    # ------------------------------------------------------------------- tools

    def get_reminder_lists(self):
        """
        List the names of all the user's reminder lists, e.g. 'Reminders', 'Groceries', 'Work'.
        Use this when the user mentions a list you haven't seen, or asks what lists they have.
        """
        script = """
        function run(argv) {
            const app = Application('Reminders');
            const names = app.lists.name();
            let fallback = '';
            try { fallback = app.defaultList.name(); } catch (e) {}
            return JSON.stringify({lists: names, default: fallback});
        }
        """
        result = self._run_jxa(script, {})
        if "error" in result:
            return result["error"]
        return result

    def add_reminder(self, title: str, due: str = "", notes: str = "", list_name: str = "", priority: str = "none"):
        """
        Add a new reminder to the macOS Reminders app, which syncs to the user's iPhone and Watch.
        due should be 'YYYY-MM-DD HH:MM' in 24-hour time, or just 'YYYY-MM-DD' for a whole day.
        priority can be 'none', 'low', 'medium' or 'high'.
        list_name is optional and defaults to the user's default list.
        Use whenever the user asks to be reminded of something, or to add a task or to-do.
        """
        if not title.strip():
            return "I need to know what the reminder is for."

        due_parts = self._parse_due(due)
        if due and not due_parts:
            return f"I couldn't understand '{due}' as a date. Use YYYY-MM-DD HH:MM."

        params = {
            "title": title.strip(),
            "notes": notes,
            "list": list_name or self.default_list or "",
            "priority": self.PRIORITY_MAP.get(str(priority).lower(), 0),
            "due": due_parts,
        }

        script = """
        function run(argv) {
            const p = JSON.parse(argv[0]);
            const app = Application('Reminders');

            let target;
            if (p.list) {
                const matches = app.lists.whose({name: p.list})();
                if (matches.length === 0) {
                    return JSON.stringify({error: 'nolist'});
                }
                target = matches[0];
            } else {
                target = app.defaultList;
            }

            const props = {name: p.title, priority: p.priority};
            if (p.notes) { props.body = p.notes; }
            if (p.due) {
                props.dueDate = new Date(p.due.year, p.due.month - 1, p.due.day, p.due.hour, p.due.minute);
            }

            const reminder = app.Reminder(props);
            target.reminders.push(reminder);

            return JSON.stringify({
                id: reminder.id(),
                name: reminder.name(),
                list: target.name(),
                due: p.due ? reminder.dueDate().toISOString() : null
            });
        }
        """
        result = self._run_jxa(script, params)
        if "error" in result:
            if result["error"] == "nolist":
                return f"There's no reminder list called '{params['list']}'. Call get_reminder_lists to see what exists."
            return result["error"]

        spoken = f"Added '{result['name']}' to {result['list']}"
        if result.get("due"):
            spoken += f", due {self._speak_due(result['due'])}"
        return spoken + "."

    def get_reminders(self, list_name: str = "", include_completed: bool = False, max_results: int = 25):
        """
        Read the user's reminders back, newest first, with their due dates and priority.
        Leave list_name empty to read across every list.
        Use when the user asks what's on their list, what they need to do, or what they're forgetting.
        """
        params = {
            "list": list_name or "",
            "includeCompleted": bool(include_completed),
            "max": max(1, int(max_results)),
        }

        script = """
        function run(argv) {
            const p = JSON.parse(argv[0]);
            const app = Application('Reminders');

            let lists;
            if (p.list) {
                lists = app.lists.whose({name: p.list})();
                if (lists.length === 0) { return JSON.stringify({error: 'nolist'}); }
            } else {
                lists = app.lists();
            }

            const out = [];
            for (const list of lists) {
                // Bulk property access — one Apple Event per property instead of one per reminder
                const names = list.reminders.name();
                if (names.length === 0) { continue; }
                const ids = list.reminders.id();
                const done = list.reminders.completed();
                const dues = list.reminders.dueDate();
                const prios = list.reminders.priority();
                const bodies = list.reminders.body();
                const listName = list.name();

                for (let i = 0; i < names.length; i++) {
                    if (!p.includeCompleted && done[i]) { continue; }
                    out.push({
                        id: ids[i],
                        title: names[i],
                        list: listName,
                        completed: done[i],
                        due: dues[i] ? dues[i].toISOString() : null,
                        priority: prios[i],
                        notes: bodies[i] || ''
                    });
                }
            }

            // Anything with a due date sorts first, soonest at the top
            out.sort(function (a, b) {
                if (a.due && b.due) { return a.due < b.due ? -1 : 1; }
                if (a.due) { return -1; }
                if (b.due) { return 1; }
                return 0;
            });

            return JSON.stringify({reminders: out.slice(0, p.max), total: out.length});
        }
        """
        result = self._run_jxa(script, params)
        if "error" in result:
            if result["error"] == "nolist":
                return f"There's no reminder list called '{list_name}'."
            return result["error"]

        reminders = result.get("reminders", [])
        if not reminders:
            return "Nothing on the list." if not list_name else f"Nothing on {list_name}."

        return [
            {
                "id": r["id"],
                "title": r["title"],
                "list": r["list"],
                "due": self._speak_due(r["due"]) if r["due"] else "no due date",
                "priority": self.PRIORITY_NAMES.get(r["priority"], "none"),
                "notes": r["notes"][:200],
                "completed": r["completed"],
            }
            for r in reminders
        ]

    def get_due_reminders(self, days: int = 1):
        """
        Get only the reminders that are due within the next N days, plus anything overdue.
        days of 1 means today and tomorrow.
        Use for 'what do I have today', 'what's due', 'what am I forgetting', or a morning briefing.
        """
        cutoff = datetime.datetime.now() + datetime.timedelta(days=max(0, int(days)))
        params = {"cutoff": cutoff.isoformat(timespec="seconds")}

        script = """
        function run(argv) {
            const p = JSON.parse(argv[0]);
            const app = Application('Reminders');
            const cutoff = new Date(p.cutoff);
            const out = [];

            for (const list of app.lists()) {
                const names = list.reminders.name();
                if (names.length === 0) { continue; }
                const ids = list.reminders.id();
                const done = list.reminders.completed();
                const dues = list.reminders.dueDate();
                const prios = list.reminders.priority();
                const listName = list.name();

                for (let i = 0; i < names.length; i++) {
                    if (done[i] || !dues[i]) { continue; }
                    if (dues[i] <= cutoff) {
                        out.push({
                            id: ids[i],
                            title: names[i],
                            list: listName,
                            due: dues[i].toISOString(),
                            priority: prios[i]
                        });
                    }
                }
            }

            out.sort(function (a, b) { return a.due < b.due ? -1 : 1; });
            return JSON.stringify({reminders: out});
        }
        """
        result = self._run_jxa(script, params)
        if "error" in result:
            return result["error"]

        reminders = result.get("reminders", [])
        if not reminders:
            return f"Nothing due in the next {days} day{'s' if days != 1 else ''}."

        now = datetime.datetime.now()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "list": r["list"],
                "due": self._speak_due(r["due"]),
                "overdue": (self._to_local(r["due"]) or now) < now,
                "priority": self.PRIORITY_NAMES.get(r["priority"], "none"),
            }
            for r in reminders
        ]

    def complete_reminder(self, title: str, list_name: str = ""):
        """
        Mark a reminder as done by its title. Matching is case-insensitive and partial,
        so 'landlord' will find 'Call the landlord'.
        Use when the user says they've finished, done or handled something.
        """
        return self._act_on_match(title, list_name, "complete")

    def delete_reminder(self, title: str, list_name: str = ""):
        """
        Delete a reminder outright by its title. This cannot be undone — prefer
        complete_reminder when the user has actually finished the task.
        Use when the user asks to remove, cancel or delete a reminder.
        """
        return self._act_on_match(title, list_name, "delete")

    def _act_on_match(self, title: str, list_name: str, action: str):
        """
        Internal helper. Finds incomplete reminders matching a title and either
        completes or deletes the single match, or reports back the ambiguity.
        """
        if not title.strip():
            return "I need to know which reminder you mean."

        params = {"query": title.strip().lower(), "list": list_name or "", "action": action}

        script = """
        function run(argv) {
            const p = JSON.parse(argv[0]);
            const app = Application('Reminders');

            let lists;
            if (p.list) {
                lists = app.lists.whose({name: p.list})();
                if (lists.length === 0) { return JSON.stringify({error: 'nolist'}); }
            } else {
                lists = app.lists();
            }

            const hits = [];
            for (const list of lists) {
                const names = list.reminders.name();
                if (names.length === 0) { continue; }
                const done = list.reminders.completed();
                const listName = list.name();

                for (let i = 0; i < names.length; i++) {
                    if (done[i]) { continue; }
                    if (names[i].toLowerCase().indexOf(p.query) !== -1) {
                        hits.push({index: i, list: list, listName: listName, title: names[i]});
                    }
                }
            }

            if (hits.length === 0) { return JSON.stringify({error: 'nomatch'}); }
            if (hits.length > 1) {
                return JSON.stringify({
                    error: 'ambiguous',
                    matches: hits.map(function (h) { return {title: h.title, list: h.listName}; })
                });
            }

            const hit = hits[0];
            const reminder = hit.list.reminders[hit.index];
            if (p.action === 'complete') {
                reminder.completed = true;
            } else {
                app.delete(reminder);
            }
            return JSON.stringify({title: hit.title, list: hit.listName});
        }
        """
        result = self._run_jxa(script, params)
        if "error" in result:
            if result["error"] == "nolist":
                return f"There's no reminder list called '{list_name}'."
            if result["error"] == "nomatch":
                return f"I couldn't find an open reminder matching '{title}'."
            if result["error"] == "ambiguous":
                options = ", ".join(f"'{m['title']}'" for m in result.get("matches", [])[:5])
                return f"That matches more than one reminder: {options}. Which did you mean?"
            return result["error"]

        verb = "Marked done" if action == "complete" else "Deleted"
        return f"{verb}: '{result['title']}' in {result['list']}."

    def update_reminder(self, title: str, new_title: str = "", due: str = "", notes: str = "", priority: str = "", list_name: str = ""):
        """
        Change an existing reminder found by its title. Only the fields you pass get changed.
        due should be 'YYYY-MM-DD HH:MM'; priority can be 'none', 'low', 'medium' or 'high'.
        Use when the user wants to push a reminder back, rename it, or change how urgent it is.
        """
        if not title.strip():
            return "I need to know which reminder to change."

        due_parts = None
        if due:
            due_parts = self._parse_due(due)
            if not due_parts:
                return f"I couldn't understand '{due}' as a date. Use YYYY-MM-DD HH:MM."

        priority_value = None
        if priority:
            if str(priority).lower() not in self.PRIORITY_MAP:
                return "Priority has to be none, low, medium or high."
            priority_value = self.PRIORITY_MAP[str(priority).lower()]

        params = {
            "query": title.strip().lower(),
            "list": list_name or "",
            "newTitle": new_title,
            "notes": notes,
            "priority": priority_value,
            "due": due_parts,
        }

        script = """
        function run(argv) {
            const p = JSON.parse(argv[0]);
            const app = Application('Reminders');

            let lists;
            if (p.list) {
                lists = app.lists.whose({name: p.list})();
                if (lists.length === 0) { return JSON.stringify({error: 'nolist'}); }
            } else {
                lists = app.lists();
            }

            const hits = [];
            for (const list of lists) {
                const names = list.reminders.name();
                if (names.length === 0) { continue; }
                const done = list.reminders.completed();
                const listName = list.name();
                for (let i = 0; i < names.length; i++) {
                    if (done[i]) { continue; }
                    if (names[i].toLowerCase().indexOf(p.query) !== -1) {
                        hits.push({index: i, list: list, listName: listName, title: names[i]});
                    }
                }
            }

            if (hits.length === 0) { return JSON.stringify({error: 'nomatch'}); }
            if (hits.length > 1) {
                return JSON.stringify({
                    error: 'ambiguous',
                    matches: hits.map(function (h) { return {title: h.title, list: h.listName}; })
                });
            }

            const hit = hits[0];
            const reminder = hit.list.reminders[hit.index];
            const changed = [];

            if (p.newTitle) { reminder.name = p.newTitle; changed.push('title'); }
            if (p.notes) { reminder.body = p.notes; changed.push('notes'); }
            if (p.priority !== null) { reminder.priority = p.priority; changed.push('priority'); }
            if (p.due) {
                reminder.dueDate = new Date(p.due.year, p.due.month - 1, p.due.day, p.due.hour, p.due.minute);
                changed.push('due date');
            }

            return JSON.stringify({
                title: reminder.name(),
                list: hit.listName,
                changed: changed,
                due: reminder.dueDate() ? reminder.dueDate().toISOString() : null
            });
        }
        """
        result = self._run_jxa(script, params)
        if "error" in result:
            if result["error"] == "nolist":
                return f"There's no reminder list called '{list_name}'."
            if result["error"] == "nomatch":
                return f"I couldn't find an open reminder matching '{title}'."
            if result["error"] == "ambiguous":
                options = ", ".join(f"'{m['title']}'" for m in result.get("matches", [])[:5])
                return f"That matches more than one reminder: {options}. Which did you mean?"
            return result["error"]

        if not result.get("changed"):
            return "Nothing was changed — no new values were given."

        spoken = f"Updated the {' and '.join(result['changed'])} on '{result['title']}'"
        if result.get("due") and "due date" in result["changed"]:
            spoken += f", now due {self._speak_due(result['due'])}"
        return spoken + "."

    def create_reminder_list(self, list_name: str):
        """
        Create a brand new reminder list, e.g. 'Groceries' or 'Work'.
        Use when the user wants to start a new list rather than add to an existing one.
        """
        if not list_name.strip():
            return "I need a name for the new list."

        script = """
        function run(argv) {
            const p = JSON.parse(argv[0]);
            const app = Application('Reminders');
            if (app.lists.whose({name: p.name})().length > 0) {
                return JSON.stringify({error: 'exists'});
            }
            const list = app.List({name: p.name});
            app.lists.push(list);
            return JSON.stringify({name: p.name});
        }
        """
        result = self._run_jxa(script, {"name": list_name.strip()})
        if "error" in result:
            if result["error"] == "exists":
                return f"You already have a list called '{list_name}'."
            return result["error"]
        return f"Created a new reminder list called '{result['name']}'."
