package vade.spike.miuix

import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.window.ComposeViewport
import kotlinx.browser.document

/**
 * Wasm entry point. ComposeViewport mounts into an existing DOM element and
 * creates its own canvas, so index.html only needs a <body>.
 */
@OptIn(ExperimentalComposeUiApi::class)
fun main() {
    ComposeViewport(document.body!!) {
        App()
    }
}
