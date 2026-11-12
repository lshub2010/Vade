// C0b T3: Kotlin/Wasm -> JS interop proof.
//
// For C-10 the only surface that matters is: Kotlin/Wasm calls a JS module that
// returns a Promise and observes the resolved value. The full Noise handshake +
// one AEAD round-trip lives behind `runHandshake()` in the bridge, so this file
// is a single interop hop rather than a dozen.

@file:OptIn(kotlin.js.ExperimentalWasmJsInterop::class)

import kotlin.js.JsAny
import kotlin.js.JsModule
import kotlin.js.unsafeCast

@JsModule("vade-bridge")
external fun runHandshake(psk: String?): JsAny

@JsModule("vade-bridge")
external fun asText(value: JsAny): String

private external interface JsPromise : JsAny {
    fun then(onFulfilled: (JsAny?) -> Unit): JsPromise
    fun `catch`(onRejected: (JsAny?) -> Unit): JsAny
}

fun main() {
    println("KOTLIN_WASM_START")
    runHandshake(null)
        .unsafeCast<JsPromise>()
        .then { value ->
            println("KOTLIN_WASM_OK " + asText(value ?: error("resolved to undefined")))
            null
        }
        .`catch` { err ->
            println("KOTLIN_WASM_FAIL " + asText(err!!))
            null
        }
}
