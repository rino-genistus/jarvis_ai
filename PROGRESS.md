# Jarvis AI

A voice-controlled personal assistant that runs locally on Apple Silicon.

## What's Built

Jarvis runs in the background as a macOS app: a lightweight listener waits for "Hey Jarvis" using openWakeWord models, then hands off to an engine that transcribes with MLX Whisper, reasons and calls tools with a local Qwen 2.5 model on Ollama, and answers aloud with Kokoro text-to-speech, while a companion status app checks every link from microphone to models and can restart it. It controls 55 tools across eight agents, from Google Calendar, Gmail, Spotify and Apple Reminders to weather, web research and everyday lookups in Contacts, Apple Calendar, Notes and exact date arithmetic, and it keeps standing preferences such as temperature units or how to address the user. Intent routing pairs a few-shot classifier with request-aware guards (34 of 35 on a labelled test set), and listening is fitted to the room by measuring its background noise and confirming real speech before transcribing, so background sound is never answered and the always-on listener stays at about 95 MB. Memory runs entirely on the device, with a Chroma vector store and local embeddings for recall, falling back to an Obsidian vault that grows into a knowledge graph of daily notes and linked topic pages, and a test harness covers wake word accuracy, intent routing, tool choice, memory, preferences and latency without touching live accounts.

## What's Next

Conversational replies will be kept from claiming actions Jarvis did not take, and the single-pass tool call will become a multi-step agent loop that can chain results, such as reading this week's emails and then blocking time on the calendar to follow up. Jarvis will use the everyday tools proactively, checking calendars and contacts before scheduling or emailing without being asked. Memory will add "last time we spoke" context at the start of each conversation and recall weighted toward recent facts, drawing on the topic graph to connect related sessions. After that come a designed interface with a menu-bar status indicator, macOS app and file control, starting at login, and a more natural ElevenLabs voice for the final release.
