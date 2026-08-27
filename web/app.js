/* NX Android Streamer — v0.1 phone web client.
 *
 * A thin client: receive one WebRTC video track of a remote Android (portrait
 * 1080x2400), display it edge to edge, and push touch back over the "input"
 * datachannel. Zero dependencies, zero external requests, no build step.
 *
 * Signaling (frozen protocol — the server is the offerer):
 *   ws://<same host:port>/ws, JSON per message.
 *     <- {"type":"offer","sdp":"..."}      => setRemoteDescription, answer
 *     -> {"type":"answer","sdp":"..."}
 *    <-> {"type":"ice","candidate":"<candidate string>","sdpMLineIndex":N}
 *   No STUN/TURN: everything rides the Tailscale network, host candidates only.
 *
 * Input datachannel (server-created, label "input"), one JSON object per message:
 *     {"t":"td","id":N,"x":F,"y":F}   touch down
 *     {"t":"tm","id":N,"x":F,"y":F}   touch move
 *     {"t":"tu","id":N}               touch up
 *     {"t":"ping","ts":<ms>}          -> server echoes {"t":"pong","ts":<ms>}
 *   x,y are normalized 0..1 against the VIDEO CONTENT, not the element.
 */

'use strict';

(function () {
  // ---------------------------------------------------------------------
  // Config
  // ---------------------------------------------------------------------

  var RECONNECT_MIN_MS = 500;    // first retry delay
  var RECONNECT_MAX_MS = 5000;   // cap
  var PING_INTERVAL_MS = 2000;   // heartbeat over the datachannel
  var PONG_TIMEOUT_MS = 8000;    // no pong for this long while live => reconnect
  var PILL_FADE_MS = 3000;       // hide the pill after this much stable live time
  var MAX_TOUCH_IDS = 10;        // wire ids are small ints 0..9

  // Verbose logging only with ?debug in the URL. Steady state is silent.
  var DEBUG = /(?:^|[?&])debug\b/.test(location.search);
  function log() {
    if (DEBUG) console.log.apply(console, ['[nx]'].concat([].slice.call(arguments)));
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var stage = document.getElementById('stage');
  var video = document.getElementById('video');
  var pill = document.getElementById('pill');

  // ---------------------------------------------------------------------
  // Session state
  //
  // Every connection attempt gets a generation number. Callbacks from a torn
  // down session compare against it and bail, so a late ws.onclose or a stale
  // ICE candidate can never schedule a second reconnect or poison the new one.
  // ---------------------------------------------------------------------

  var gen = 0;
  var ws = null;
  var pc = null;
  var dc = null;              // "input" datachannel, created by the server
  var retryDelay = RECONNECT_MIN_MS;
  var retryTimer = null;
  var pingTimer = null;
  var lastPongAt = 0;
  var rtt = null;
  var live = false;           // media flowing and datachannel open
  var stopped = false;        // page is going away; stop retrying
  var attempts = 0;           // connection attempts this page load

  // ---------------------------------------------------------------------
  // Status pill
  // ---------------------------------------------------------------------

  var pillFadeTimer = null;
  var pillText = '';

  function setPill(text, waiting) {
    if (text !== pillText) {
      pill.textContent = text;
      pillText = text;
    }
    pill.classList.toggle('waiting', !!waiting);
  }

  // Show the pill and, when we're live and stable, fade it back out.
  function revealPill(sticky) {
    clearTimeout(pillFadeTimer);
    pill.classList.remove('faded');
    if (!sticky) {
      pillFadeTimer = setTimeout(function () {
        if (live) pill.classList.add('faded');
      }, PILL_FADE_MS);
    }
  }

  var lastPhase = null;

  // phase: 'connecting' | 'reconnecting' | 'live'
  function setPhase(phase) {
    var changed = phase !== lastPhase;
    lastPhase = phase;

    if (phase === 'live') {
      document.body.classList.add('live');
      setPill(rtt == null ? 'live' : rtt + ' ms', false);
      if (changed) revealPill(false);
    } else {
      document.body.classList.remove('live');
      setPill(phase === 'reconnecting' ? 'reconnecting…' : 'connecting…', true);
      if (changed) revealPill(true);
    }
  }

  function refreshRttReadout() {
    if (!live) return;
    setPill(rtt == null ? 'live' : rtt + ' ms', false);
  }

  // ---------------------------------------------------------------------
  // Connection lifecycle
  // ---------------------------------------------------------------------

  function wsURL() {
    var scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return scheme + '//' + location.host + '/ws';
  }

  function connect() {
    if (stopped) return;
    teardown();                       // paranoia: never run two sessions
    var myGen = ++gen;
    attempts++;

    // First attempt of the page load reads as "connecting…"; anything after a
    // failure or a dropped stream reads as "reconnecting…".
    setPhase(attempts === 1 ? 'connecting' : 'reconnecting');
    log('connecting', myGen);

    try {
      // No iceServers: host candidates over the VPN are all we need.
      pc = new RTCPeerConnection({ iceServers: [] });
    } catch (err) {
      console.error('[nx] RTCPeerConnection unavailable', err);
      scheduleReconnect(myGen);
      return;
    }

    pc.ontrack = function (ev) {
      if (myGen !== gen) return;
      var stream = (ev.streams && ev.streams[0]) || new MediaStream([ev.track]);
      if (video.srcObject !== stream) {
        video.srcObject = stream;
        // Autoplay of a muted video is allowed; a rejection here is benign
        // (usually a race with a teardown) so it stays quiet.
        var p = video.play();
        if (p && p.catch) p.catch(function () {});
      }
      markLiveIfReady();
    };

    // The server owns the datachannel; we just pick it up.
    pc.ondatachannel = function (ev) {
      if (myGen !== gen) return;
      if (ev.channel.label !== 'input') return;
      dc = ev.channel;
      dc.onopen = function () {
        if (myGen !== gen) return;
        log('input channel open');
        lastPongAt = Date.now();
        startPing(myGen);
        markLiveIfReady();
      };
      dc.onmessage = function (ev2) {
        if (myGen !== gen) return;
        onInputMessage(ev2.data);
      };
      dc.onclose = function () {
        if (myGen !== gen) return;
        log('input channel closed');
        scheduleReconnect(myGen);
      };
      dc.onerror = function () { /* surfaced via onclose */ };
    };

    pc.onicecandidate = function (ev) {
      if (myGen !== gen) return;
      // null candidate = end-of-candidates; the protocol has no frame for it.
      if (!ev.candidate || !ev.candidate.candidate) return;
      send({
        type: 'ice',
        candidate: ev.candidate.candidate,
        sdpMLineIndex: ev.candidate.sdpMLineIndex
      });
    };

    pc.onconnectionstatechange = function () {
      if (!pc || myGen !== gen) return;
      var st = pc.connectionState;
      log('pc', st);
      if (st === 'failed' || st === 'closed') {
        scheduleReconnect(myGen);
      } else if (st === 'disconnected') {
        // 'disconnected' can self-heal; give ICE a moment before we nuke it.
        setTimeout(function () {
          if (myGen === gen && pc && pc.connectionState === 'disconnected') {
            scheduleReconnect(myGen);
          }
        }, 2000);
      } else if (st === 'connected') {
        markLiveIfReady();
      }
    };

    try {
      ws = new WebSocket(wsURL());
    } catch (err) {
      console.error('[nx] websocket failed to open', err);
      scheduleReconnect(myGen);
      return;
    }

    ws.onopen = function () {
      if (myGen !== gen) return;
      log('ws open, waiting for offer');
    };

    ws.onmessage = function (ev) {
      if (myGen !== gen) return;
      var msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (err) {
        console.warn('[nx] non-JSON signaling frame ignored');
        return;
      }
      onSignal(msg, myGen);
    };

    ws.onerror = function () { /* a close always follows; handled there */ };

    ws.onclose = function () {
      if (myGen !== gen) return;
      log('ws closed');
      scheduleReconnect(myGen);
    };
  }

  function onSignal(msg, myGen) {
    if (!msg || !msg.type) return;

    if (msg.type === 'offer') {
      var desc = { type: 'offer', sdp: msg.sdp };
      pc.setRemoteDescription(desc)
        .then(function () { return pc.createAnswer(); })
        .then(function (answer) { return pc.setLocalDescription(answer); })
        .then(function () {
          if (myGen !== gen) return;
          send({ type: 'answer', sdp: pc.localDescription.sdp });
          log('answer sent');
        })
        .catch(function (err) {
          if (myGen !== gen) return;
          console.error('[nx] negotiation failed', err);
          scheduleReconnect(myGen);
        });
      return;
    }

    if (msg.type === 'ice') {
      if (!msg.candidate) return;   // tolerate an end-of-candidates marker
      var cand;
      try {
        cand = new RTCIceCandidate({
          candidate: msg.candidate,
          sdpMLineIndex: msg.sdpMLineIndex
        });
      } catch (err) {
        console.warn('[nx] bad ICE candidate ignored');
        return;
      }
      // Candidates can legitimately arrive before setRemoteDescription resolves;
      // the browser queues them, and a rejection here is not fatal.
      pc.addIceCandidate(cand).catch(function (err) {
        if (myGen === gen) log('addIceCandidate rejected', err && err.message);
      });
      return;
    }

    if (msg.type === 'answer') return;  // we are never the offerer
    log('unhandled signal', msg.type);
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  function markLiveIfReady() {
    if (live) return;
    var mediaOk = !!video.srcObject;
    var chanOk = !!dc && dc.readyState === 'open';
    var pcOk = !!pc && (pc.connectionState === 'connected' || pc.iceConnectionState === 'connected' || pc.iceConnectionState === 'completed');
    if (mediaOk && chanOk && pcOk) {
      live = true;
      retryDelay = RECONNECT_MIN_MS;   // a good connection resets the backoff
      setPhase('live');
      log('live');
    }
  }

  function startPing(myGen) {
    clearInterval(pingTimer);

    function tick() {
      if (myGen !== gen) return;
      if (!dc || dc.readyState !== 'open') return;
      // A dead link that never closes cleanly (phone slept, NAT rebind) shows up
      // as pongs going missing long before the socket notices.
      if (live && Date.now() - lastPongAt > PONG_TIMEOUT_MS) {
        log('pong timeout');
        scheduleReconnect(myGen);
        return;
      }
      try {
        dc.send(JSON.stringify({ t: 'ping', ts: Date.now() }));
      } catch (err) {
        scheduleReconnect(myGen);
      }
    }

    tick();                            // seed the RTT readout immediately
    pingTimer = setInterval(tick, PING_INTERVAL_MS);
  }

  function onInputMessage(data) {
    if (typeof data !== 'string') return;
    var msg;
    try {
      msg = JSON.parse(data);
    } catch (err) {
      return;
    }
    if (msg && msg.t === 'pong') {
      lastPongAt = Date.now();
      if (typeof msg.ts === 'number') {
        rtt = Math.max(0, Math.round(Date.now() - msg.ts));
        refreshRttReadout();
      }
    }
  }

  function teardown() {
    clearInterval(pingTimer);
    pingTimer = null;
    live = false;
    rtt = null;
    releaseAllPointers();

    if (dc) {
      dc.onopen = dc.onmessage = dc.onclose = dc.onerror = null;
      try { dc.close(); } catch (err) { /* already gone */ }
      dc = null;
    }
    if (ws) {
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      try { ws.close(); } catch (err) { /* already gone */ }
      ws = null;
    }
    if (pc) {
      pc.ontrack = pc.ondatachannel = pc.onicecandidate = pc.onconnectionstatechange = null;
      try { pc.close(); } catch (err) { /* already gone */ }
      pc = null;
    }
    // Keep the last frame on screen rather than flashing to void mid-reconnect;
    // srcObject is replaced when the next track arrives.
  }

  function scheduleReconnect(myGen) {
    if (myGen !== undefined && myGen !== gen) return;
    if (stopped) return;
    if (retryTimer) return;           // one retry in flight is enough

    teardown();
    gen++;                            // invalidate every callback from the dead session
    setPhase('reconnecting');

    var delay = retryDelay;
    retryDelay = Math.min(retryDelay * 2, RECONNECT_MAX_MS);
    log('reconnect in', delay, 'ms');

    retryTimer = setTimeout(function () {
      retryTimer = null;
      connect();
    }, delay);
  }

  // Reconnect right now, skipping the backoff wait (used when the page comes
  // back to the foreground or the network returns).
  function reconnectNow() {
    if (stopped) return;
    clearTimeout(retryTimer);
    retryTimer = null;
    retryDelay = RECONNECT_MIN_MS;
    connect();
  }

  // ---------------------------------------------------------------------
  // Letterbox math
  //
  // The <video> fills the viewport with object-fit: contain, so the actual
  // picture is a centered rect inside the element, with dead bars on two sides
  // (top/bottom if the element is taller than the content's aspect, left/right
  // if wider). Input must be normalized against the PICTURE, not the element,
  // or every touch is offset by the bar size.
  //
  //   scale   = min(elemW / videoW, elemH / videoH)   <- contain picks the smaller
  //   contentW = videoW * scale,  contentH = videoH * scale
  //   offsetX  = (elemW - contentW) / 2               <- contain centers
  //   offsetY  = (elemH - contentH) / 2
  //   nx = (clientX - rect.left - offsetX) / contentW
  //   ny = (clientY - rect.top  - offsetY) / contentH
  //
  // Then clamp to [0,1]: a touch that starts in the picture and drags into a
  // dead bar must keep reporting the edge, not vanish — dropping it would strand
  // a finger down on the remote side. Same for a touch that begins in a bar.
  // ---------------------------------------------------------------------

  var rectCache = null;

  function invalidateRect() { rectCache = null; }

  function contentRect() {
    if (rectCache) return rectCache;

    var r = video.getBoundingClientRect();
    var vw = video.videoWidth;
    var vh = video.videoHeight;

    // Before the first frame we have no intrinsic size; fall back to the
    // element box so early touches are still roughly right.
    if (!vw || !vh) {
      rectCache = { left: r.left, top: r.top, w: r.width || 1, h: r.height || 1 };
      return rectCache;
    }

    var scale = Math.min(r.width / vw, r.height / vh);
    var cw = vw * scale;
    var ch = vh * scale;
    rectCache = {
      left: r.left + (r.width - cw) / 2,
      top: r.top + (r.height - ch) / 2,
      w: cw || 1,
      h: ch || 1
    };
    return rectCache;
  }

  function clamp01(v) {
    return v < 0 ? 0 : v > 1 ? 1 : v;
  }

  function normX(clientX) {
    var c = contentRect();
    return clamp01((clientX - c.left) / c.w);
  }

  function normY(clientY) {
    var c = contentRect();
    return clamp01((clientY - c.top) / c.h);
  }

  // ---------------------------------------------------------------------
  // Touch -> datachannel
  //
  // Browsers hand out large, ever-increasing pointerIds; the wire wants small
  // slot ids (0..9) that map to Android's touch slots. Keep a free list.
  // ---------------------------------------------------------------------

  var pointerSlots = new Map();       // pointerId -> wire id
  var freeSlots = [];
  for (var i = MAX_TOUCH_IDS - 1; i >= 0; i--) freeSlots.push(i);

  function acquireSlot(pointerId) {
    if (pointerSlots.has(pointerId)) return pointerSlots.get(pointerId);
    if (!freeSlots.length) return -1; // more than 10 fingers: ignore the extras
    var id = freeSlots.pop();
    pointerSlots.set(pointerId, id);
    return id;
  }

  function releaseSlot(pointerId) {
    if (!pointerSlots.has(pointerId)) return -1;
    var id = pointerSlots.get(pointerId);
    pointerSlots.delete(pointerId);
    freeSlots.push(id);
    return id;
  }

  function releaseAllPointers() {
    pointerSlots.forEach(function (id) { freeSlots.push(id); });
    pointerSlots.clear();
  }

  function sendInput(obj) {
    if (!dc || dc.readyState !== 'open') return;
    try {
      dc.send(JSON.stringify(obj));
    } catch (err) {
      // A send on a channel that died between the check and here is not worth
      // a log line; the close handler will reconnect.
    }
  }

  stage.addEventListener('pointerdown', function (ev) {
    ev.preventDefault();
    onFirstInteraction();
    invalidateRect();

    var id = acquireSlot(ev.pointerId);
    if (id < 0) return;
    try { stage.setPointerCapture(ev.pointerId); } catch (err) { /* fine */ }
    sendInput({ t: 'td', id: id, x: normX(ev.clientX), y: normY(ev.clientY) });
  }, { passive: false });

  stage.addEventListener('pointermove', function (ev) {
    if (!pointerSlots.has(ev.pointerId)) return;   // hover / mouse with no button
    ev.preventDefault();
    var id = pointerSlots.get(ev.pointerId);

    // Coalesced events carry every sample the OS captured between frames, so a
    // fast swipe arrives as a real curve instead of two far-apart points.
    var samples = null;
    if (typeof ev.getCoalescedEvents === 'function') {
      try { samples = ev.getCoalescedEvents(); } catch (err) { samples = null; }
    }
    if (!samples || !samples.length) samples = [ev];

    for (var i = 0; i < samples.length; i++) {
      var s = samples[i];
      sendInput({ t: 'tm', id: id, x: normX(s.clientX), y: normY(s.clientY) });
    }
  }, { passive: false });

  function endPointer(ev) {
    if (!pointerSlots.has(ev.pointerId)) return;
    if (ev.cancelable) ev.preventDefault();
    var id = releaseSlot(ev.pointerId);
    try { stage.releasePointerCapture(ev.pointerId); } catch (err) { /* fine */ }
    if (id >= 0) sendInput({ t: 'tu', id: id });
  }

  stage.addEventListener('pointerup', endPointer, { passive: false });
  // pointercancel: the browser stole the gesture (system back swipe, call UI).
  // Lift on the remote side too, or the finger stays stuck down forever.
  stage.addEventListener('pointercancel', endPointer, { passive: false });

  // Belt and braces against stuck fingers when focus or visibility is yanked.
  window.addEventListener('blur', liftEverything);
  function liftEverything() {
    pointerSlots.forEach(function (id) { sendInput({ t: 'tu', id: id }); });
    releaseAllPointers();
  }

  // Suppress the gestures the browser would otherwise eat.
  stage.addEventListener('contextmenu', function (ev) { ev.preventDefault(); });
  document.addEventListener('gesturestart', function (ev) { ev.preventDefault(); });
  document.addEventListener('dragstart', function (ev) { ev.preventDefault(); });
  document.addEventListener('touchmove', function (ev) {
    if (ev.cancelable) ev.preventDefault();
  }, { passive: false });

  // Layout changes move the content rect.
  window.addEventListener('resize', invalidateRect);
  window.addEventListener('scroll', invalidateRect, true);
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', invalidateRect);
  }
  video.addEventListener('loadedmetadata', function () {
    invalidateRect();
    log('video', video.videoWidth + 'x' + video.videoHeight);
  });
  video.addEventListener('resize', invalidateRect);

  // ---------------------------------------------------------------------
  // Fullscreen + wake lock
  //
  // Both need a user gesture, so they hang off the first tap. Fullscreen can be
  // dismissed later (system gesture, notification shade) — the next tap retries.
  // ---------------------------------------------------------------------

  var wakeLock = null;

  function onFirstInteraction() {
    requestFullscreenIfNeeded();
    requestWakeLock();
  }

  function isFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function requestFullscreenIfNeeded() {
    if (isFullscreen()) return;
    var fn = stage.requestFullscreen || stage.webkitRequestFullscreen;
    if (!fn) return;
    try {
      var p = fn.call(stage, { navigationUI: 'hide' });
      // Rejection is normal (no gesture, unsupported, already exiting): stay quiet.
      if (p && p.catch) p.catch(function () {});
    } catch (err) { /* degrade silently */ }
  }

  document.addEventListener('fullscreenchange', function () {
    invalidateRect();
    log('fullscreen', isFullscreen());
  });

  function requestWakeLock() {
    // Wake Lock needs a secure context; over plain http on the VPN it simply
    // won't be there. Degrade silently — the user can raise the screen timeout.
    if (!navigator.wakeLock || wakeLock) return;
    try {
      navigator.wakeLock.request('screen').then(function (lock) {
        wakeLock = lock;
        lock.addEventListener('release', function () { wakeLock = null; });
        log('wake lock held');
      }).catch(function () { wakeLock = null; });
    } catch (err) {
      wakeLock = null;
    }
  }

  // ---------------------------------------------------------------------
  // Sleep / foreground handling
  //
  // The phone locking kills timers and usually the socket; coming back must not
  // need a reload. On visible: re-take the wake lock, re-check the pipe, and
  // reconnect immediately (no backoff wait) if anything is down.
  // ---------------------------------------------------------------------

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') {
      liftEverything();               // don't leave fingers down across a sleep
      return;
    }
    invalidateRect();
    requestWakeLock();

    var wsOk = ws && ws.readyState === WebSocket.OPEN;
    var dcOk = dc && dc.readyState === 'open';
    var pcOk = pc && pc.connectionState !== 'failed' && pc.connectionState !== 'closed';
    if (!wsOk || !dcOk || !pcOk) {
      log('woke up into a dead session, reconnecting');
      reconnectNow();
    } else {
      lastPongAt = Date.now();        // don't trip the watchdog on sleep time
      var p = video.play();
      if (p && p.catch) p.catch(function () {});
    }
  });

  window.addEventListener('online', function () {
    if (!live) reconnectNow();
  });

  window.addEventListener('pagehide', function () {
    stopped = true;
    liftEverything();
    teardown();
  });

  // Coming back from the bfcache (browser back onto this page) resurrects the
  // document with everything torn down and `stopped` latched. Un-latch and
  // rebuild rather than leaving a dead page that needs a manual reload.
  window.addEventListener('pageshow', function (ev) {
    if (!ev.persisted) return;
    stopped = false;
    attempts = 0;
    lastPhase = null;
    reconnectNow();
  });

  // ---------------------------------------------------------------------
  // Go
  // ---------------------------------------------------------------------

  setPhase('connecting');
  connect();
})();
