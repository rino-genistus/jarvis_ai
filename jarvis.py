from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
import os
import speech_recognition as sr
import mlx_whisper 
from ollama import chat, ChatResponse
import time
import tempfile
from agents import Calendar_Agents, WebSearchAgents, WeatherSearch, SpotifyAgent, GmailAgent
from datetime import datetime
from kokoro import KPipeline
import sounddevice as sd
import numpy as np
from pinecone import Pinecone
import threading

start_time = time.time()

load_dotenv()

current_date = datetime.now().strftime("%A, %B %d, %Y")
print(current_date)

pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
index_name = 'jarvis-ai'
if not pc.has_index(index_name):
    pc.create_index_for_model(
        name=index_name,
        cloud="aws",
        region="us-east-1",
        embed={
            "model":"llama-text-embed-v2",
            "field_map":{"text": "chunk_text"}
        }
    )

dense_index = pc.Index(index_name)

eleven_labs = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

kokoro_pipeline = None
kokoro_ready = threading.Event()

def load_kokoro():
    global kokoro_pipeline
    kokoro_pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
    kokoro_ready.set()
    print(f"Kokoro Loaded")

threading.Thread(target=load_kokoro, daemon=True).start()

calendar = Calendar_Agents()
websearch = WebSearchAgents()
weather = WeatherSearch()
spotify = SpotifyAgent()
gmail = GmailAgent()

system_prompt = f"""
    You are JARVIS (Just A Rather Very Intelligent System), an advanced AI assistant built to serve as a highly capable, loyal, and intelligent personal assistant.

    ## Context
    Today's date is {current_date}. Use this for any scheduling, calendar, or time-related tasks.

    ## Personality
    You speak with quiet confidence and calm authority. Your tone is casual but sharp — like a trusted right-hand who knows you well and doesn't waste your time. You have a dry wit that surfaces naturally, never forced. You are direct, precise, and never pad responses with fluff. You treat your user with the kind of familiar respect a close, highly competent aide would — you anticipate their needs and you're always in their corner.

    ## Communication Style
    - Respond conversationally. You are being spoken to out loud, so your responses must sound natural when heard, not read. No bullet points, headers, or markdown — speak in sentences.
    - Be concise. Get to the point. Skip affirmations like "Certainly!", "Of course!", or "Great question!" — just answer.
    - Match the energy of the request. Quick question gets a quick answer. Deep problem gets a thorough response.
    - If you don't know something, say so plainly and offer to find out. Never fabricate.
    - After delivering information, briefly invite the user to go deeper or ask a follow-up — one short sentence, never pushy.

    ## Capabilities
    You help with research, analysis, writing, coding, planning, scheduling, and reasoning through problems. When given tools, you use them efficiently and report back with only what's relevant.

    ## Memory
    You only know what has been said in this conversation. You have no memory between sessions and no access to anything outside of the current conversation. Your slate is blank until the user tells you something — if asked about prior context, say so plainly.

    ## Core Principles
    - Your user's goals are your goals. You advocate for their success.
    - You are proactive — if you notice something relevant, you mention it without being asked.
    - You do not moralize, lecture, or add unsolicited caveats. You trust your user's judgment.
    - You are never sycophantic. Honest, direct assessment beats flattery every time.
    - When something is outside your ability, say so immediately and suggest alternatives.
    - You have no memory between sessions. Never invent prior context, projects, people, or history that hasn't been explicitly stated in this conversation.
    - When starting fresh with no context, greet the user briefly and ask what they need. Nothing more.

    Respond only with your spoken reply. No meta-commentary, no explaining what you're about to do — just do it.
"""
messages = [{"role": "system", "content": system_prompt}]

"""EXIT_PHRASES = [
    # Direct goodbyes
    "goodbye", "good bye", "bye", "bye bye", "farewell",
    
    # Dismissals
    "that's all", "that is all", "that'll be all", "that will be all",
    "you're dismissed", "dismissed",
    
    # Sleep/standby commands
    "go to sleep", "sleep mode", "stand by", "standby",
    "power down", "shut down", "shutdown",
    
    # Session enders
    "we're done", "we are done", "i'm done", "i am done",
    "end session", "stop listening", "stop jarvis",
    "that's enough", "that is enough", "enough for now",
    
    # Natural conversation closers
    "talk later", "talk to you later", "we'll talk later",
    "catch you later", "until next time",
    
    # Explicit exits
    "exit", "quit", "close",
]"""

#classifier = pipeline("zero-shot-classification", model="typeform/distilbart-mnli-12-3")

candidate_labels = ["end_conversation", "continue_conversation"]

r = sr.Recognizer()

def play_chime():
    """
    Function that Plays a Small Chime sound so that User knows when Jarvis is done talking
    """
    sample_rate = 24000
    duration = 0.15
    freq = 880
    t = np.linspace(0, duration, int(sample_rate * duration))
    tone = (np.sin(2 * np.pi * freq * t) * 0.3).astype(np.float32)
    sd.play(tone, samplerate=sample_rate)
    sd.wait()

def play_audio_with_kokoro(text):
    """
    Kokoro model that plays the text returned by the LLM
    """
    kokoro_ready.wait()
    generator = kokoro_pipeline(text, voice='af_heart')
    chunks = []
    for i, (gs, ps, audio) in enumerate(generator):
        chunks.append(audio)
    
    if chunks:
        full_audio = np.concatenate(chunks)
        sd.play(full_audio, samplerate=24000)
        sd.wait()
    play_chime() #Chimes right before the User can talk. Make it sound a bit better later

def play_audio_with_text_eleven_labs(text):
    """
    Not being used for right now, takes up money. But will be used for final release. Plays audio of Jarvis with text from LLM
    """
    audio = eleven_labs.text_to_speech.convert(
        text=text,
        voice_id="k7IRoeykhdGZUkTeJ1ID",
        model_id="eleven_turbo_v2_5",
        output_format="mp3_44100_128",
    )
    audio_bytes = b"".join(audio)
    print("Jarvis Talking Now")
    play(audio=audio_bytes)

def record_audio_and_transcribe_elevenlabs():
    """
    Not being used for right now, takes up money. But will be used for final release. Records user's prompt and request and transcribes for LLM usage
    """
    with sr.Microphone() as source:
        r.adjust_for_ambient_noise(source, duration=0.5)
        r.energy_threshold = 300
        r.pause_threshold = 0.8
        print("User Talks Now")
        audio_text = r.listen(source)
        wav_audio_data = audio_text.get_wav_data()
        transcription = eleven_labs.speech_to_text.convert(
            file = wav_audio_data,
            model_id="scribe_v2",
            tag_audio_events=True,
            language_code="eng",
            diarize=True,
        )
        return transcription.text
    
def record_audio_and_transcribe_mlx_whisper():
    """
    Current transcription method for user - free. Runs efficiently on Mac Silicone chip
    """
    with sr.Microphone() as source:
        r.energy_threshold = 200
        r.pause_threshold = 1.5
        r.phrase_threshold = 0.1
        r.non_speaking_duration = 0.8
        print("User Talks Now")
        audio_text = r.listen(source, timeout=10, phrase_time_limit=45)
        wav_audio_data = audio_text.get_wav_data()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_audio_data)
            temp_path = f.name

        result = mlx_whisper.transcribe(
            temp_path,
            path_or_hf_repo="mlx-community/whisper-small-mlx",
        )
        
        os.remove(temp_path)
        print("User: ", result['text'].strip())
        return result["text"].strip()

def extract_important_messages(messages):
    """
    Given the messages from the current chat, uses qwen2.5:7b model to review all messages and retreive list of meaningful messages
    """
    response = chat(
        model='qwen2.5:7b',
        messages=[
            {
                "role": "user",
                "content": f"""Review this conversation and extract only information worth remembering long-term about the user — preferences, facts, habits, goals, or anything personally relevant. 
                Ignore greetings, small talk, and one-off questions like weather lookups.
                Return a list of concise factual statements, one per line. If nothing is worth remembering, return 'NONE'.
                
                Conversation:
                {messages}"""
            }
        ]
    )
    results = response.message.content.strip()
    if results == "NONE":
        return []
    memories = [
        line.strip().lstrip("0123456789.-) ")
        for line in results.split('\n') 
        if line.strip()
    ]
    
    records = [
        {"id": f"mem-{int(time.time())}-{i}", "chunk_text": memory}
        for i, memory in enumerate(memories)
        if memory
    ]
    
    print(f"Storing {len(records)} memories: {[r['chunk_text'] for r in records]}")
    return records

def retrieve_memories(query: str, top_k: int = 5):
    """
    Retrieves most meaningful messages from Pinecone Vector DB for conversation context
    """
    results = dense_index.search(
        namespace="jarvis-memory-namespace",
        query={"inputs": {"text": query}, "top_k": top_k},
        fields=["chunk_text"]
    )
    memories = [hit["fields"]["chunk_text"] for hit in results["result"]["hits"]]
    return memories

def classify_intent(text):
    """
    classifies intent for LLM to know how to proceed with conversation
    """
    """Returns 'exit', 'tool', or 'chat'"""
    response = chat(
            model='llama3.2:1b',
            messages=[{"role": "user", "content": 
                    f"""Classify this message. Reply with exactly one word only: exit, tool, or chat.

            exit = user wants to end the conversation
            tool = user wants real-world action or data (weather, calendar, spotify, web search)
            chat = general conversation or questions

            Message: "{text}"
            
            One word answer:"""}]
    )
    result = response.message.content.strip().lower()
    first_word = result.split()[0] if result else "chat"
    if first_word not in ("exit", "tool", "chat"):
        return "chat"
    return first_word

def safe_speak(text):
    """
    Makes sure that something is returned from tool calls so that TTS doesnt play empty sound files
    """
    try:
        if not text or not text.strip():
            print("Warning: empty response, skipping TTS")
            return
        play_audio_with_kokoro(text)
    except KeyboardInterrupt:
        pass

def main_loop():
    """
    Main Program Loop
    """
    with sr.Microphone() as source:
        print("Calibrating microphone...")
        r.adjust_for_ambient_noise(source, duration=0.3)
        r.dynamic_energy_threshold = False
    #List of available tools for LLM
    available_functions = {
        'create_event': calendar.create_event,
        'get_calendar_events': calendar.get_calendar_events,
        'update_calendar_event': calendar.update_calendar_event,
        'delete_calendar_event': calendar.delete_calendar_event,
        'search_web': websearch.search_web,
        'extract_webpages': websearch.extract_webpages,
        'get_current_weather': weather.get_current_weather,
        'get_weather_with_time': weather.get_weather_with_time,
        'get_current_track': spotify.get_current_track,
        'search_song_and_queue': spotify.search_song_and_queue,
        'add_song_to_playlist': spotify.add_song_to_playlist,
        'create_playlist': spotify.create_playlist,
        'recently_played': spotify.recently_played,
        'skip_song': spotify.skip_song,
        'pause_song': spotify.pause_song,
        'shuffle': spotify.shuffle,
        'set_volume': spotify.set_volume,
    }
    while True:
        spoken = ""
        transcribed_text = record_audio_and_transcribe_mlx_whisper() #User Text
        intent = classify_intent(transcribed_text) #Intent for LLM
        memories = retrieve_memories(transcribed_text) #List of meaningful memories from previous conversations
        if memories:
            memory_block = "\n".join(f"- {m}" for m in memories)
            messages[0]["content"] = system_prompt + f"\n\n## What you know about the user:\n{memory_block}" #Adding meaningful memories to message history for LLM context
        print(f"Intent: {intent}") 
        messages.append({"role": "user", "content": transcribed_text})
        if intent == 'exit':
            #User is leaving or conversation is done
            completion = chat(model="qwen2.5:7b", messages=messages)
            spoken = completion.message.content or ""
            messages.append({"role": "assistant", "content": spoken})
            safe_speak(spoken)
            mems_list = extract_important_messages(messages=messages) #Gets meaningful messages from conversation
            if mems_list:
                #Uploading meaningful memories to pinecone
                dense_index.upsert_records(namespace="jarvis-memory-namespace",records=mems_list)
            break

        elif intent == 'tool':
            #Needs tool usage
            safe_speak("Right away sir.")

            current_date = datetime.now().strftime("%Y-%m-%d")
            dated_messages = messages[:-1] + [{
                "role": "user", 
                "content": f"[Today's date is {current_date}] {messages[-1]['content']}"
            }]
            response: ChatResponse = chat(
                model='qwen2.5:7b',
                messages=dated_messages,
                tools=[
                    calendar.create_event, 
                    calendar.get_calendar_events, 
                    calendar.delete_calendar_event, 
                    calendar.update_calendar_event, 
                    websearch.search_web, 
                    websearch.extract_webpages,
                    weather.get_current_weather,
                    weather.get_weather_with_time,
                    spotify.get_current_track,
                    spotify.search_song_and_queue,
                    spotify.skip_song,
                    spotify.pause_song,
                    spotify.shuffle,
                    spotify.set_volume,
                ],
            )
            messages.append({"role": "assistant", "content": response.message.content or ""})

            if response.message.tool_calls: #Loops through all required tool calls to finish task
                for tool_call in response.message.tool_calls:
                    if tool_call.function.name in available_functions:
                        print(f"Calling {tool_call.function.name} with {tool_call.function.arguments}")
                        result = available_functions[tool_call.function.name](**tool_call.function.arguments) #Calls tool calls to complete task
                        print(f"Tool result: {result}")
                        messages.append({"role": "tool", "tool_name": tool_call.function.name, "content": str(result)})

                messages.append({
                    "role": "user",
                    "content": "Summarize the tool results naturally in Jarvis's voice. Deliver the key points concisely, then offer one natural follow-up — like whether they want more detail on anything specific or if they want any action taken on the information given."
                }) #Summarizes what was just done

                follow_up: ChatResponse = chat(model='qwen2.5:7b', messages=messages)
                spoken = follow_up.message.content or ""
                messages.append({"role": "assistant", "content": spoken})
            else:
                spoken = response.message.content or response.message.thinking or ""

        else:  # chat
            response: ChatResponse = chat(model='qwen2.5:7b', messages=messages)
            spoken = response.message.content or ""
            messages.append({"role": "assistant", "content": spoken})

        print("Jarvis:", spoken)
        safe_speak(spoken)
        #time.sleep(0.5)


"""def contains_exit_phrase(transcribed_text):
    return any(phrase in transcribed_text for phrase in EXIT_PHRASES)"""

kokoro_ready.wait()
#main_loop()
print(f"First Command: {time.time() - start_time:.2f}s")
#spotify.shuffle(False)
emails = gmail.get_unread_emails()
for email in emails:
    print(email.get("id"))
#print(gmail.get_all_labels())
#print(gmail.get_email_by_id('19db13ec2dccb18a'))
#print(gmail.send_email("Hello, Test email", "rr1406@scarletmail.rutgers.edu", 'rgenistus@gmail.com'))
print(gmail.get_sender_profile())
#print(gmail.search_email(query="from: rinogenistus@gmail.com"))
print(gmail.reply_to_email(email_id="19dbca5b6b521fab"))