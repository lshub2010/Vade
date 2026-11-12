// C0a · T3 acceptance demo.
//
// `./gradlew run` must print HANDSHAKE_OK plus the callback event JSON. Every
// number printed here comes from the Rust core: the Kotlin side only marshals
// strings, bytes and callbacks across the UniFFI boundary.

package demo

import kotlinx.coroutines.runBlocking
import uniffi.protocol_spike.EventSink
import uniffi.protocol_spike.SpikeException
import uniffi.protocol_spike.clearEventSink
import uniffi.protocol_spike.closeSession
import uniffi.protocol_spike.handshake
import uniffi.protocol_spike.sendEncrypted
import uniffi.protocol_spike.sessionCount
import uniffi.protocol_spike.setEventSink
import uniffi.protocol_spike.version
import java.io.File
import java.net.ServerSocket
import java.util.Collections
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

private val events = Collections.synchronizedList(mutableListOf<String>())

/** Callback implementation: Rust calls this from its own worker threads. */
private class PrintingSink : EventSink {
    override fun onEvent(json: String) {
        events.add(json)
        println("EVENT $json")
    }
}

private fun freeClosedPort(): Int = ServerSocket(0).use { it.localPort }

private fun startServer(serverBin: File, keyDir: File): Pair<Process, String> {
    check(serverBin.canExecute()) { "missing server binary: ${serverBin.absolutePath}" }
    val proc = ProcessBuilder(serverBin.absolutePath)
        .redirectErrorStream(true)
        .apply { environment()["VADE_SPIKE_KEY_DIR"] = keyDir.absolutePath }
        .start()
    var addr: String? = null
    val reader = proc.inputStream.bufferedReader()
    val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10)
    while (System.nanoTime() < deadline) {
        val line = reader.readLine() ?: break
        when {
            line.startsWith("LISTEN=") -> addr = line.removePrefix("LISTEN=").trim()
            line.startsWith("SERVER_PUB=") -> println("SERVER_PUB=${line.removePrefix("SERVER_PUB=").trim()}")
            else -> println("SERVER_LOG $line")
        }
        if (addr != null) break
    }
    checkNotNull(addr) { "server did not announce LISTEN= within 10s" }
    return proc to addr
}

fun main() {
    try {
        runBlocking { demo() }
    } catch (t: Throwable) {
        println("DEMO_FAILED: ${t::class.simpleName}: ${t.message}")
        exitProcess(1)
    }
}

private suspend fun demo() {
    val keyDir = File(System.getProperty("vade.spike.keyDir") ?: "/tmp/vade-spike-keys")
    val serverBin = File(
        System.getProperty("vade.spike.serverBin")
            ?: System.getenv("VADE_SPIKE_SERVER_BIN")
            ?: "protocol-spike/target/debug/spike-server"
    )
    println("BINDING_KOTLIN version=${version()}")
    check(System.getenv("VADE_SPIKE_SERVER_PUB") != null) { "VADE_SPIKE_SERVER_PUB is not set" }

    val (server, addr) = startServer(serverBin, keyDir)
    try {
        setEventSink(PrintingSink())

        val report = handshake(addr)
        println("HANDSHAKE_JSON=$report")
        check("\"ok\":true" in report) { "handshake did not report ok: $report" }
        check("Noise_IK_25519_ChaChaPoly_BLAKE2s" in report) { "wrong protocol in report: $report" }
        println("SESSION_COUNT_AFTER_HANDSHAKE=${sessionCount()}")
        check(sessionCount() == 1u) { "expected exactly one live session" }

        val plaintext = "hello vade from kotlin".toByteArray()
        val echoed = sendEncrypted(addr, plaintext)
        check(echoed.contentEquals(plaintext)) { "echo mismatch: ${echoed.size} bytes" }
        println("ECHO_OK bytes=${echoed.size}")

        // Closing the session must be observable from Rust, and the event sink
        // must have received it.
        check(closeSession(addr)) { "closeSession returned false" }
        println("SESSION_COUNT_AFTER_CLOSE=${sessionCount()}")

        // Error path: a closed port must surface as a typed exception whose
        // `code` survives the FFI boundary (C-04 UNAVAILABLE).
        val deadPort = freeClosedPort()
        val code = try {
            handshake("127.0.0.1:$deadPort")
            "NO_ERROR"
        } catch (e: SpikeException) {
            when (e) {
                is SpikeException.Io -> e.code
                is SpikeException.Crypto -> e.code
                is SpikeException.Config -> e.code
                is SpikeException.Protocol -> e.code
            }
        }
        println("ERROR_CODE=$code")
        check(code == "UNAVAILABLE") { "expected UNAVAILABLE for a refused connection, got $code" }

        clearEventSink()
        val kinds = events.map { Regex("\"event\":\"([^\"]+)\"").find(it)?.groupValues?.get(1) ?: "?" }
        println("EVENT_KINDS=${kinds.joinToString(",")}")
        println("EVENT_COUNT=${events.size}")
        check(events.isNotEmpty()) { "callback sink received nothing" }
        check(kinds.contains("handshake.completed")) { "missing handshake.completed event" }
        check(kinds.contains("session.closed")) { "missing session.closed event" }

        println("HANDSHAKE_OK")
    } finally {
        server.destroy()
        server.waitFor(5, TimeUnit.SECONDS)
        server.destroyForcibly()
    }
}
