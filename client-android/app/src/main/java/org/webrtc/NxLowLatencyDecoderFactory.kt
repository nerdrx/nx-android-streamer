// Deliberately in package org.webrtc: AndroidVideoDecoder and the
// MediaCodecWrapper interfaces are package-private, and injecting a wrapper is
// the only way to reach MediaCodec.configure() before the decoder starts.
//
// Why this exists: stock libwebrtc configures MediaCodec with default latency
// behaviour, which lets the decoder hold several frames in flight for
// smoothness. For a remote phone that buffering IS the lag you feel between
// tapping and seeing. KEY_LOW_LATENCY (API 30+) tells the codec to emit each
// frame as soon as it is decoded. moonlight-android does the same thing for
// the same reason — see BORROWED.md.
package org.webrtc

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCrypto
import android.media.MediaFormat
import android.os.Build
import android.util.Log
import android.view.Surface
import java.io.IOException
import java.nio.ByteBuffer

private const val TAG = "nx-dec"

/**
 * Wraps a real MediaCodec and stamps low-latency options onto the format on the
 * way through. Everything else is a straight pass-through.
 */
private class LowLatencyCodec(private val codec: MediaCodec) : MediaCodecWrapper {

    override fun configure(format: MediaFormat, surface: Surface?, crypto: MediaCrypto?, flags: Int) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        }
        // Pre-30 devices (and vendors that ignore KEY_LOW_LATENCY) often honour
        // the older vendor keys instead. Setting them is harmless when unknown.
        format.setInteger("vdec-lowlatency", 1)
        format.setInteger("vendor.qti-ext-dec-low-latency.enable", 1)
        // Never let the decoder run ahead building a pretty queue.
        format.setInteger(MediaFormat.KEY_PRIORITY, 0)          // 0 = realtime
        codec.configure(format, surface, crypto, flags)
    }

    override fun start() = codec.start()
    override fun flush() = codec.flush()
    override fun stop() = codec.stop()
    override fun release() = codec.release()
    override fun dequeueInputBuffer(timeoutUs: Long): Int = codec.dequeueInputBuffer(timeoutUs)
    override fun queueInputBuffer(index: Int, offset: Int, size: Int, ptsUs: Long, flags: Int) =
        codec.queueInputBuffer(index, offset, size, ptsUs, flags)

    override fun dequeueOutputBuffer(info: MediaCodec.BufferInfo, timeoutUs: Long): Int =
        codec.dequeueOutputBuffer(info, timeoutUs)

    override fun releaseOutputBuffer(index: Int, render: Boolean) =
        codec.releaseOutputBuffer(index, render)

    override fun getInputFormat(): MediaFormat = codec.inputFormat
    override fun getOutputFormat(): MediaFormat = codec.outputFormat
    override fun getOutputFormat(index: Int): MediaFormat = codec.getOutputFormat(index)
    override fun getInputBuffer(index: Int): ByteBuffer = codec.getInputBuffer(index)!!
    override fun getOutputBuffer(index: Int): ByteBuffer = codec.getOutputBuffer(index)!!
    override fun createInputSurface(): Surface = codec.createInputSurface()
    override fun setParameters(params: android.os.Bundle) = codec.setParameters(params)
    override fun getCodecInfo(): MediaCodecInfo = codec.codecInfo
}

private class LowLatencyCodecFactory : MediaCodecWrapperFactory {
    @Throws(IOException::class)
    override fun createByCodecName(name: String): MediaCodecWrapper =
        LowLatencyCodec(MediaCodec.createByCodecName(name))
}

/**
 * Hardware decoders configured for latency, with libwebrtc's own software
 * decoders as the fallback so the client still works where H.264 hardware
 * decode is missing (x86 emulators, oddball devices).
 */
class NxLowLatencyDecoderFactory(
    private val eglContext: EglBase.Context?
) : VideoDecoderFactory {

    private val hardware = MediaCodecVideoDecoderFactory(eglContext, Predicate { info ->
        // Hardware only; the software path is handled separately below.
        !info.name.startsWith("OMX.google.", ignoreCase = true) &&
            !info.name.startsWith("c2.android.", ignoreCase = true)
    })
    private val software = SoftwareVideoDecoderFactory()

    override fun createDecoder(codec: VideoCodecInfo): VideoDecoder? {
        val mime = VideoCodecMimeType.valueOf(codec.name.uppercase())
        val info = findHardwareCodec(mime)
        if (info != null) {
            Log.d(TAG, "low-latency hardware decoder: ${info.name} for ${codec.name}")
            return AndroidVideoDecoder(
                LowLatencyCodecFactory(), info.name, mime,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible, eglContext
            )
        }
        Log.d(TAG, "no hardware decoder for ${codec.name}; falling back to software")
        return software.createDecoder(codec)
    }

    override fun getSupportedCodecs(): Array<VideoCodecInfo> {
        val seen = LinkedHashMap<String, VideoCodecInfo>()
        hardware.supportedCodecs.forEach { seen[it.name.lowercase()] = it }
        software.supportedCodecs.forEach { seen.putIfAbsent(it.name.lowercase(), it) }
        return seen.values.toTypedArray()
    }

    private fun findHardwareCodec(mime: VideoCodecMimeType): MediaCodecInfo? {
        val list = android.media.MediaCodecList(android.media.MediaCodecList.REGULAR_CODECS)
        return list.codecInfos.firstOrNull { info ->
            !info.isEncoder &&
                info.supportedTypes.any { it.equals(mime.mimeType(), ignoreCase = true) } &&
                !info.name.startsWith("OMX.google.", ignoreCase = true) &&
                !info.name.startsWith("c2.android.", ignoreCase = true)
        }
    }
}
