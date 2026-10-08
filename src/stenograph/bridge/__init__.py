"""Jigasi bridge: streaming transcription for Jitsi conferences.

Jigasi (docker-jitsi-meet's `transcriber` service) connects to this app over
websocket and streams per-participant audio; we answer with caption messages
(partial/final). Protocol verified against `jitsi/skynet` streaming_whisper
and Jigasi's WhisperWebsocket.java — see `protocol.py` for the wire format.
"""
