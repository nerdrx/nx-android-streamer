package dev.nerdrx.nxandroidstreamer

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.nio.charset.StandardCharsets

/**
 * Browses `_nxstream._tcp` over mDNS/DNS-SD (the daemon advertises it on the
 * stream port). Zero typing on the same network: one tap on a discovered card
 * connects and saves the profile.
 *
 * The host we surface is the TXT `addr=<ip>` record, NOT NsdManager's resolved
 * host — the resolved name can be a `.local` the phone can route on LAN but never
 * over Tailscale, while `addr` is the routable IP the daemon chose to publish.
 */
class Discovery(context: Context) {

    data class Server(val name: String, val host: String, val port: Int, val w: Int, val h: Int) {
        val key get() = "$host:$port"
    }

    interface Listener {
        fun onServersChanged(servers: List<Server>)
    }

    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val main = Handler(Looper.getMainLooper())
    private val found = LinkedHashMap<String, Server>()   // service name -> server
    private var listener: Listener? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var running = false

    // NSD (pre-API 31) allows only one resolve in flight; serialize them.
    private val resolveQueue = ArrayDeque<NsdServiceInfo>()
    private var resolving = false

    fun start(listener: Listener) {
        if (running) return
        this.listener = listener
        found.clear()
        val dl = object : NsdManager.DiscoveryListener {
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "start discovery failed: $errorCode")
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onDiscoveryStarted(serviceType: String) { Log.d(TAG, "discovery started") }
            override fun onDiscoveryStopped(serviceType: String) { Log.d(TAG, "discovery stopped") }
            override fun onServiceFound(info: NsdServiceInfo) {
                if (info.serviceType?.contains("nxstream") == true || info.serviceType?.contains(SERVICE_TYPE.trimEnd('.')) == true) {
                    enqueueResolve(info)
                }
            }
            override fun onServiceLost(info: NsdServiceInfo) {
                main.post {
                    if (found.remove(info.serviceName) != null) emit()
                }
            }
        }
        discoveryListener = dl
        running = true
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, dl)
        } catch (e: Exception) {
            Log.w(TAG, "discoverServices threw: ${e.message}")
            running = false
        }
    }

    fun stop() {
        if (!running) return
        running = false
        discoveryListener?.let { try { nsd.stopServiceDiscovery(it) } catch (_: Exception) {} }
        discoveryListener = null
        listener = null
        found.clear()
        resolveQueue.clear()
        resolving = false
    }

    private fun enqueueResolve(info: NsdServiceInfo) {
        main.post {
            resolveQueue.addLast(info)
            pumpResolve()
        }
    }

    private fun pumpResolve() {
        if (resolving) return
        val next = resolveQueue.removeFirstOrNull() ?: return
        resolving = true
        val rl = object : NsdManager.ResolveListener {
            override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                main.post {
                    resolving = false
                    // ALREADY_ACTIVE: requeue and try again shortly.
                    if (errorCode == NsdManager.FAILURE_ALREADY_ACTIVE) {
                        resolveQueue.addLast(info)
                        main.postDelayed({ pumpResolve() }, 250)
                    } else {
                        pumpResolve()
                    }
                }
            }
            override fun onServiceResolved(info: NsdServiceInfo) {
                main.post {
                    resolving = false
                    ingest(info)
                    pumpResolve()
                }
            }
        }
        try {
            nsd.resolveService(next, rl)
        } catch (e: Exception) {
            resolving = false
            main.postDelayed({ pumpResolve() }, 250)
        }
    }

    private fun ingest(info: NsdServiceInfo) {
        val attrs = info.attributes ?: emptyMap()
        fun txt(k: String): String? = attrs[k]?.let { String(it, StandardCharsets.UTF_8) }
        // Prefer the published addr= over the resolved (possibly .local) host.
        val host = txt("addr")?.takeIf { it.isNotBlank() }
            ?: info.host?.hostAddress
            ?: return
        val port = if (info.port in 1..65535) info.port else Prefs.DEFAULT_PORT
        val w = txt("w")?.toIntOrNull() ?: 0
        val h = txt("h")?.toIntOrNull() ?: 0
        val name = info.serviceName?.takeIf { it.isNotBlank() } ?: host
        found[info.serviceName ?: host] = Server(name, host, port, w, h)
        emit()
    }

    private fun emit() {
        listener?.onServersChanged(found.values.toList())
    }

    companion object {
        private const val TAG = "nx-nsd"
        private const val SERVICE_TYPE = "_nxstream._tcp."
    }
}
