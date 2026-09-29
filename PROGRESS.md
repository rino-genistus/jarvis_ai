# Jarvis AI

A voice-controlled personal assistant that runs locally on Apple Silicon.

## What's Built

Jarvis runs in the background as a macOS app: a lightweight listener waits for "Hey Jarvis" using openWakeWord models, then hands off to an engine that transcribes with MLX Whisper, reasons and calls tools with a local Qwen 2.5 model on Ollama, and answers aloud with Kokoro text-to-speech. It controls 45 tools across six agents (Google Calendar, Gmail, Spotify, Apple Reminders, OpenWeatherMap and Tavily web research), each registered automatically from the agent's methods, and any service without credentials is switched off cleanly instead of being guessed at. Intent routing pairs a few-shot classifier with request-aware guards, scoring 28 of 29 on a labelled test set (up from 13), while streamed speech, prewarmed models, cache-aware prompts and a two-process design keep replies fast and the always-on listener at about 95 MB. Each conversation is distilled into long-term memory, recalled through a Pinecone vector index and written into an Obsidian vault as a growing knowledge graph of daily notes and linked topic pages, and a test harness covers wake word accuracy, intent routing, tool choice, memory output and latency without touching live accounts.

## What's Next

Relative dates like "next Friday" will be parsed deterministically, and conversational replies will be kept from promising actions Jarvis cannot take. The single-pass tool call will become a multi-step agent loop that can chain results, such as reading this week's emails and then blocking time on the calendar to follow up. Memory will add "last time we spoke" context at the start of each conversation and recall weighted toward recent facts, drawing on the topic graph to connect related sessions. After that come a designed interface with a menu-bar status indicator, macOS app and file control, starting at login, and a more natural ElevenLabs voice for the final release.
