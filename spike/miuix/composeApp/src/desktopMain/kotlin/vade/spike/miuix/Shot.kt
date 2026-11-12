package vade.spike.miuix

import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.ImageComposeScene
import androidx.compose.ui.unit.Density
import org.jetbrains.skia.EncodedImageFormat
import java.io.File

/**
 * Headless renderer: no X server, no Skiko window. Useful on CI where there is
 * no display; the result is the same composables the real window draws.
 *
 * Note: Compose Desktop dialogs live in their own window, so the `dialog`
 * section may come out without the popup layer here. The Xvfb pass covers that.
 */
@OptIn(ExperimentalComposeUiApi::class)
fun main(args: Array<String>) {
    val outDir = File(args.firstOrNull() ?: "shots").apply { mkdirs() }
    val width = 480
    val height = 420
    var written = 0
    for (section in Section.entries) {
        val scene = ImageComposeScene(width = width, height = height, density = Density(1f)) {
            App(section)
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
