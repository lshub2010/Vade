// Baseline control group for the wasm size number: the same 8 sections drawn
// with Material 3 only (no Miuix). Same Kotlin/CMP versions as :composeApp so
// the delta is attributable to the skin, not to the toolchain.
plugins {
    kotlin("multiplatform")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.compose")
}

kotlin {
    @OptIn(org.jetbrains.kotlin.gradle.ExperimentalWasmDsl::class)
    wasmJs {
        outputModuleName = "baselineApp"
        browser {
            commonWebpackConfig {
                outputFileName = "baselineApp.js"
            }
        }
        binaries.executable()
    }

    sourceSets {
        commonMain.dependencies {
            implementation(compose.runtime)
            implementation(compose.foundation)
            implementation(compose.material3)
            implementation(compose.ui)
        }
    }
}
