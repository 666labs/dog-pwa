# ===========================================================================
# PATCHED COPY OF A PIP-INSTALLED VENDOR FILE — NOT a normal module of this
# repo. Nothing here imports from this directory; this file exists only to be
# reviewed and then COPIED over the installed original on the Ascent GX10:
#
#   /home/asus/dimos-env/lib/python3.12/site-packages/unitree_webrtc_connect/webrtc_video.py
#
# (dev-laptop equivalent, which this copy was taken from:
#   /home/alex/miniconda3/envs/dimos/lib/python3.11/site-packages/unitree_webrtc_connect/webrtc_video.py)
#
# It will be silently reverted by any `pip install`/upgrade of
# unitree-webrtc-connect — re-apply after one.
#
# THE ONLY CHANGE vs. upstream is the last line of track_handler(): the
# except clause logged `{e}`, which renders as an EMPTY STRING for aiortc's
# bare `MediaStreamError()` (raised with no args when a track ends). Every
# real video-track death therefore logged the identical, content-free line
# "Error in callback <function accept_track...>: " — the single most useful
# log line during the 2026-07-25 silent-WebRTC-death incidents said nothing at
# all. `{e!r}` always shows the exception's type even when it carries no
# message, and exc_info=True attaches the traceback that tells you where in
# the recv() path it died. Logging-only: no behaviour change.
# ===========================================================================
import logging
from .webrtc_datachannel import WebRTCDataChannel
from aiortc import RTCPeerConnection

class WebRTCVideoChannel:
    def __init__(self, pc:RTCPeerConnection, datachannel:WebRTCDataChannel) -> None:
        self.pc = pc
        self.pc.addTransceiver("video", direction="recvonly")
        self.datachannel = datachannel
        # List to hold multiple callbacks
        self.track_callbacks = []
    
    def switchVideoChannel(self, switch: bool):
        self.datachannel.switchVideoChannel(switch)
    
    def add_track_callback(self, callback):
        """
        Adds a callback to be triggered when an audio track is received.
        """
        if callable(callback):
            self.track_callbacks.append(callback)
        else:
            logging.warning(f"Callback {callback} is not callable.")  
    
    async def track_handler(self, track):
        logging.info("Receiving video frame")
        # Trigger all registered callbacks
        for callback in self.track_callbacks:
            try:
                # Call each callback function and pass the track
                await callback(track)
            except Exception as e:
                logging.error(f"Error in callback {callback}: {e!r}", exc_info=True)
    