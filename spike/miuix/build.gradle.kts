// Version matrix is pinned on purpose; see spike/miuix/versions.lock.md.
// Kotlin 2.4.20 + CMP 1.12.0 are the exact pair Miuix 0.9.4 was published against
// (checked in miuix-ui-0.9.4.module: kotlin-stdlib 2.4.20, compose foundation 1.12.0).
// Using an older Kotlin would fail on the library's 2.4 metadata version.
// Gradle is pinned to the host image's 8.10.2, which caps AGP at 8.8.x
// (AGP 8.9+ requires Gradle 8.11.1). compileSdk 36 on AGP 8.8.2 needs
// `android.suppressUnsupportedCompileSdk=36` in gradle.properties.
plugins {
    kotlin("multiplatform") version "2.4.20" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.4.20" apply false
    id("org.jetbrains.compose") version "1.12.0" apply false
    id("com.android.application") version "8.8.2" apply false
}
