# Jarvis AI

A voice-controlled personal assistant that runs locally on Apple Silicon.

## What's Built

Jarvis runs in the background as a macOS app: a lightweight listener waits for "Hey Jarvis" using openWakeWord models, then hands off to an engine that transcribes with MLX Whisper, reasons and calls tools with a local Qwen 2.5 model on Ollama, and answers aloud with Kokoro text-to-speech. It controls 45 tools across six agents (Google Calendar, Gmail, Spotify, Apple Reminders, OpenWeatherMap and Tavily web research), and each agent's methods register as tools automatically. Streamed speech, prewarmed models, cache-aware prompts and keyword-first tool routing removed prompt-processing delays of more than 13 seconds per command, and a two-process design shuts the heavy engine down when idle so the always-on listener needs only about 95 MB and a fraction of one efficiency core. Each conversation is distilled into long-term memory, recalled through a Pinecone vector index and written into an Obsidian vault as a growing knowledge graph of daily notes and linked topic pages, and a test harness covers wake word accuracy, memory output, tool routing and latency without touching live accounts.

## What's Next

Intent routing and date handling will be hardened so questions that need live data always reach a tool and phrases like "next Friday" resolve to the right day. The single-pass tool call will become a multi-step agent loop that can chain results, such as reading this week's emails and then blocking time on the calendar to follow up. Memory will add "last time we spoke" context at the start of each conversation and recall weighted toward recent facts, drawing on the topic graph to connect related sessions. After that come a designed interface with a menu-bar status indicator, macOS app and file control, starting at login, and a more natural ElevenLabs voice for the final release.
