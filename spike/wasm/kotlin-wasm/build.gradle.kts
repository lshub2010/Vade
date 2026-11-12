plugins {
    kotlin("multiplatform") version "2.2.21"
}

repositories {
    maven("https://maven.aliyun.com/repository/public")
    mavenCentral()
}

kotlin {
    wasmJs {
        nodejs()
        binaries.executable()
    }

    sourceSets {
        val wasmJsMain by getting {
            dependencies {
                implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
                // Local npm packages: the wasm-bindgen output and the thin JS bridge.
                implementation(npm("protocol-wasm", "file:../protocol-wasm/pkg"))
                implementation(npm("vade-bridge", "file:./npm/vade-bridge"))
                implementation(npm("ws", "8.18.3"))
            }
        }
    }
}
