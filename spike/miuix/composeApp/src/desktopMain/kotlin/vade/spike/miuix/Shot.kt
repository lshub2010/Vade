package vade.spike.miuix

import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.ImageComposeScene
import androidx.compose.ui.unit.Density
import org.jetbrains.skia.EncodedImageFormat
import java.io.File

/**
 * Headless renderer: no display, no Skiko window. Useful on CI; the result is the
 * same composables the real window draws.
 *
 * Dialog: `OverlayDialog` does not use a platform popup window at all — it registers
 * a `DialogState`, and Miuix's `MiuixPopupHost` (present only inside Miuix's
 * `Scaffold`, which `App()` now applies to the dialog section) draws it through an
 * `AnimatedVisibility` enter transition. So it is a normal in-composition overlay and
 * renders here too, as long as the scene is advanced past the enter animation. The
 * first frame is alpha 0, which is why a single `render()` looked blank and was
 * earlier misread as "Desktop cannot draw OverlayDialog".
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
                // Real sheet: App() puts the dialog section in a Miuix Scaffold and opens it.
                App(section, dialogOpenOnStart = true)
            } else {
                App(section)
            }
        }
        try {
            scene.render() // first frame at t=0: composes, starts enter transitions (alpha 0)
            scene.render(600_000_000L) // applies the dialog state registered during frame 1
            val image = scene.render(1_200_000_000L) // past OverlayDialog's enter animation
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
