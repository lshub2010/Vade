// C0b T3: Kotlin/Wasm -> JS interop proof.
//
// Kotlin/Wasm `@JsModule` external functions compile to `module.default(args)`,
// so the bridge's default export is a single async function returning an object.
// Kotlin reads the result through a property (`text`), which is the same shape
// C-10 will need for JS -> Kotlin return values.

@file:OptIn(kotlin.js.ExperimentalWasmJsInterop::class)

import kotlin.js.JsAny
import kotlin.js.JsModule
import kotlin.js.unsafeCast

@JsModule("vade-bridge")
external fun runHandshake(psk: String?): JsAny

private external interface BridgeResult : JsAny {
    val text: String
}

private external interface JsPromise : JsAny {
    fun then(onFulfilled: (JsAny?) -> Unit): JsPromise
    fun `catch`(onRejected: (JsAny?) -> Unit): JsPromise
}

fun main() {
    println("KOTLIN_WASM_START")
    runHandshake(null)
        .unsafeCast<JsPromise>()
        .then { value ->
            val result = value!!.unsafeCast<BridgeResult>()
            println("KOTLIN_WASM_OK " + result.text)
            null
        }
        .`catch` { err ->
            println("KOTLIN_WASM_FAIL " + (err?.toString() ?: "rejected"))
            null
        }
}
