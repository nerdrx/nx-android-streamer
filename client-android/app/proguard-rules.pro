# Minify is off for v0.2, so these rules are inert today. Kept so v0.3 can turn
# R8 on without hunting for the WebRTC JNI keep rules.
-keep class org.webrtc.** { *; }
-dontwarn org.webrtc.**
-keep class dev.nerdrx.nxandroidstreamer.** { *; }
