# Jarvis AI

A voice-controlled personal assistant that runs locally on Apple Silicon.

## What's Built

Jarvis listens through MLX Whisper, reasons and calls tools with a local Qwen 2.5 model on Ollama, and answers out loud with Kokoro text-to-speech. It controls 45 tools across six agents (Google Calendar, Gmail, Spotify, Apple Reminders, OpenWeatherMap and Tavily web research), and each agent's methods register as tools automatically. A latency overhaul using sentence-by-sentence streamed speech, a dedicated audio thread, prewarmed models, cache-aware prompts and keyword-first tool routing removed prompt-processing delays measured at more than 13 seconds per command. Long-term memory in a Pinecone vector index recalls relevant facts about the user on every turn, and a test harness checks tool registration, routing, tool choice and latency without touching live accounts.

## What's Next

An always-on wake word will let Jarvis idle in the background and go back to listening after each conversation instead of exiting. The single-pass tool call will become a multi-step agent loop that can chain results, such as reading this week's emails and then blocking time on the calendar to follow up. Memory will gain summaries that carry across sessions, recall weighted toward recent facts, and trimming of long conversations to keep responses fast. After that come macOS app and file control, a menu-bar status indicator, and a more natural ElevenLabs voice for the final release.
