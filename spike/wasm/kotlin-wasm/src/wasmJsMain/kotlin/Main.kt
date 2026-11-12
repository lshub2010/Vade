// C0b T3: Kotlin/Wasm -> JS interop proof.
//
// What this file demonstrates (and what C-10 will copy):
//   1. `@JsModule` + `external fun` imports an ES module built by wasm-bindgen;
//   2. a `suspend fun await(JsAny)` bridges a JS Promise into Kotlin coroutines;
//   3. Uint8Array moves in and out of Kotlin without copies at the Kotlin level;
//   4. a Kotlin lambda is handed to Rust as the core->UI event callback.
//
// Run: ./gradlew wasmJsNodeDevelopmentRun

package vade.spike

import kotlin.js.JsAny
import kotlin.js.JsModule
import kotlin.js.JsName
import kotlin.js.unsafeCast
import kotlinx.coroutines.DelicateCoroutinesApi
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

// 000102...1f — built rather than written out so the value cannot be
// accidentally normalised by tooling.
private val PSK_HEX = (0..31).joinToString("") { it.toString(16).padStart(2, '0') }

@JsModule("vade-bridge")
external fun startEchoServer(): JsAny

@JsModule("vade-bridge")
external fun makeTransport(url: String, remoteStaticKeyHex: String): JsAny

@JsModule("vade-bridge")
external fun exchange(transport: JsAny, bytes: JsAny): JsAny

@JsModule("vade-bridge")
external fun handshake(transport: JsAny, pskHex: String?): JsAny

@JsModule("vade-bridge")
external fun encrypt(plaintext: JsAny): JsAny

@JsModule("vade-bridge")
external fun decrypt(ciphertext: JsAny): JsAny

@JsModule("vade-bridge")
external fun onEvent(callback: (JsAny) -> Unit): JsAny

@JsModule("vade-bridge")
external fun textEncode(text: String): JsAny

@JsModule("vade-bridge")
external fun textDecode(bytes: JsAny): String

@JsModule("vade-bridge")
external fun asText(value: JsAny): String

private external interface EchoServer {
    val url: String
    val responderPub: String
    val pskHex: String
    fun close()
}

private external interface CoreEvent {
    val name: String
    val payload: String
}

private external interface JsPromise {
    fun then(onFulfilled: (JsAny?) -> Unit): JsPromise
    fun `catch`(onRejected: (JsAny?) -> Unit): JsPromise
}

@JsName("process")
private external val nodeProcess: NodeProcess

private external interface NodeProcess {
    fun exit(code: Int)
}

/** The one piece of glue C-10 needs: JS Promise -> Kotlin suspend. */
private suspend fun awaitJs(promise: JsAny): JsAny = suspendCancellableCoroutine { cont ->
    promise
        .unsafeCast<JsPromise>()
        .then { value -> cont.resume(value ?: error("promise resolved to undefined")) }
        .`catch` { error -> cont.resumeWithException(IllegalStateException(asText(error ?: error("rejected")))) }
}

@OptIn(DelicateCoroutinesApi::class)
fun main() {
    val events = mutableListOf<String>()

    GlobalScope.launch {
        try {
            val server = awaitJs(startEchoServer()).unsafeCast<EchoServer>()
            println("KOTLIN_SERVER ${server.url}")

            awaitJs(
                onEvent { raw ->
                    val event = raw.unsafeCast<CoreEvent>()
                    events += event.name
                    println("KOTLIN_EVENT ${event.name} ${event.payload}")
                },
            )

            val transport = awaitJs(makeTransport(server.url, server.responderPub))
            val info = asText(awaitJs(handshake(transport, server.pskHex)))
            println("KOTLIN_HANDSHAKE_OK $info")

            val ciphertext = awaitJs(encrypt(textEncode("hello from kotlin/wasm")))
            val echo = awaitJs(exchange(transport, ciphertext))
            val plaintext = textDecode(awaitJs(decrypt(echo)))
            println("KOTLIN_ECHO $plaintext")

            check(plaintext == "hello from kotlin/wasm") { "echo mismatch" }
            check(events.contains("handshake.done")) { "no handshake.done event" }
            check(events.contains("frame.received")) { "no frame.received event" }

            println("KOTLIN_WASM_OK events=$events")
            server.close()
            nodeProcess.exit(0)
        } catch (t: Throwable) {
            println("KOTLIN_WASM_FAIL $t")
            nodeProcess.exit(1)
        }
    }
}
