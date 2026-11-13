package vade.spike.miuix

import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application
import androidx.compose.ui.window.rememberWindowState

/**
 * Desktop entry point. `--section=<slug>` renders a single showcase section,
 * which is what the Xvfb screenshot pass uses.
 */
fun main(args: Array<String>) {
    val slug = args.firstOrNull { it.startsWith("--section=") }?.substringAfter('=')
    val section = slug?.let { s -> Section.entries.firstOrNull { it.slug == s } }

    application {
        Window(
            onCloseRequest = ::exitApplication,
            title = "Vade · Miuix spike",
            state = rememberWindowState(width = 480.dp, height = 900.dp),
        ) {
            App(section)
        }
    }
}
