package vade.spike.miuix

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.ImageComposeScene
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.Density
import org.jetbrains.skia.EncodedImageFormat
import top.yukonga.miuix.kmp.theme.MiuixTheme
import top.yukonga.miuix.kmp.theme.lightColorScheme
import java.io.File

/**
 * Headless renderer: no display, no Skiko window. Useful on CI; the result is the
 * same composables the real window draws, with one exception.
 *
 * Exception — dialog: `OverlayDialog` never gets a popup window on Desktop, so it
 * does not paint here either (same root cause as the real window, not a headless
 * quirk). The dialog section is therefore rendered through [DialogSurfaceShot],
 * which composes the dialog's surface/content directly onto the canvas. That PNG
 * is a content-surface render, NOT a real popup — see RESULT.md §三.
 *
 * args: [0] output dir, [1] optional section slug (renders just that one).
 */
@OptIn(ExperimentalComposeUiApi::class)
fun main(args: Array<String>) {
    val outDir = File(args.firstOrNull() ?: "shots").apply { mkdirs() }
    val only = args.getOrNull(1)?.takeIf { it.isNotBlank() }
    val width = 480
    val height = 420
    var written = 0
    val sections = Section.entries.filter { only == null || it.slug == only }
    require(sections.isNotEmpty()) { "unknown section '$only'" }
    for (section in sections) {
        val scene = ImageComposeScene(width = width, height = height, density = Density(1f)) {
            if (section == Section.Dialog) {
                // Bypasses OverlayDialog: only its body can be drawn on this canvas.
                MiuixTheme(colors = lightColorScheme()) {
                    Box(Modifier.fillMaxSize().background(MiuixTheme.colorScheme.background)) {
                        DialogSurfaceShot()
                    }
                }
            } else {
                App(section)
            }
        }
        try {
            val image = scene.render()
            val data = image.encodeToData(EncodedImageFormat.PNG) ?: error("PNG encode failed for ${section.slug}")
            File(outDir, "miuix-desktop-${section.slug}.png").writeBytes(data.bytes)
            written++
            println("shot ${section.slug} -> ${width}x$height ${data.bytes.size} bytes")
        } finally {
            scene.close()
        }
    }
    println("wrote $written shots to ${outDir.absolutePath}")
}
