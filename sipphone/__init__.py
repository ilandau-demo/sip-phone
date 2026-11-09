"""A small, dependency-light SIP 2.0 (RFC 3261) softphone.

Modules
-------
message   - SIP request/response parsing and serialisation
digest    - HTTP Digest authentication (RFC 2617 / RFC 7616, MD5)
sdp       - SDP offer/answer for G.711 audio + telephone-event
transport - UDP transport with the Via/rport conventions SIP expects
rtp       - RTP packetisation, G.711 (PCMU/PCMA) codec, RFC 4733 DTMF
audio     - microphone / speaker I/O (sounddevice), with a silent fallback
phone     - the user agent: registration, calls, dialog state machine
cli       - an interactive command line front end
"""

__version__ = "2.4.5"
