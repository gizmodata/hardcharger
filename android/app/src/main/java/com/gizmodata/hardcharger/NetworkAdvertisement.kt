package com.gizmodata.hardcharger

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo

/** Same DNS-SD service and metadata as the iOS app. */
class NetworkAdvertisement(context: Context) {
    private val manager = context.getSystemService(NsdManager::class.java)
    private var listener: NsdManager.RegistrationListener? = null
    fun start(port: Int) {
        stop()
        val service = NsdServiceInfo().apply {
            serviceName = "Hardcharger on Android"
            serviceType = "_hardcharger._tcp."
            setPort(port)
            setAttribute("path", "/chargewire"); setAttribute("tls", "1"); setAttribute("ui", "/ui/")
        }
        val registration = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) = Unit
            override fun onRegistrationFailed(info: NsdServiceInfo, code: Int) = Unit
            override fun onServiceUnregistered(info: NsdServiceInfo) = Unit
            override fun onUnregistrationFailed(info: NsdServiceInfo, code: Int) = Unit
        }
        listener = registration
        manager.registerService(service, NsdManager.PROTOCOL_DNS_SD, registration)
    }
    fun stop() { listener?.let { runCatching { manager.unregisterService(it) } }; listener = null }
}
