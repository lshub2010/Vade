// C0a · T3: call the UniFFI Kotlin binding from a plain JVM main().
//
// The native crate is built by Gradle (see build.gradle.kts) so that
// `./gradlew run` is a single command. The demo starts the Rust test server as a
// child process, performs one real Noise IK handshake, sends one encrypted
// message and prints the callback events. Nothing here is a mock.

import java.io.File

plugins {
    kotlin("jvm") version "2.0.21"
    application
}

val crateDir = projectDir.parentFile.resolve("protocol-spike")
val nativeLibDir = crateDir.resolve("target/debug")
val keyDir = layout.buildDirectory.dir("spike-keys").get().asFile

val cargoBin: String = System.getenv("CARGO")
    ?: File(System.getProperty("user.home"), ".cargo/bin/cargo")
        .takeIf { it.canExecute() }?.absolutePath
    ?: "cargo"

repositories {
    mavenCentral()
}

dependencies {
    // The generated bindings use JNA "direct mapping" (Native.register).
    implementation("net.java.dev.jna:jna:5.14.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.9.0")
}

kotlin {
    jvmToolchain(21)
}

application {
    mainClass.set("demo.MainKt")
}

// ---------------------------------------------------------------------------
// Native side: build the crate, then generate throwaway key material.
// ---------------------------------------------------------------------------

val cargoBuild by tasks.registering(Exec::class) {
    description = "Build the Rust cdylib + demo binaries (debug)."
    workingDir = crateDir
    commandLine(cargoBin, "build", "--bin", "spike-server", "--bin", "spike-fixture")
    inputs.files(fileTree(crateDir.resolve("src")))
    inputs.files(crateDir.resolve("Cargo.toml"), crateDir.resolve("Cargo.lock"))
    outputs.files(
        nativeLibDir.resolve("libprotocol_spike.so"),
        nativeLibDir.resolve("spike-server"),
        nativeLibDir.resolve("spike-fixture"),
    )
}

val spikeKeys by tasks.registering(Exec::class) {
    description = "Generate throwaway Noise keypairs for the demo (0600 secret files)."
    dependsOn(cargoBuild)
    workingDir = crateDir
    commandLine(nativeLibDir.resolve("spike-fixture").absolutePath, keyDir.absolutePath)
    outputs.dir(keyDir)
}

tasks.named<JavaExec>("run") {
    dependsOn(spikeKeys)
    systemProperty("jna.library.path", nativeLibDir.absolutePath)
    systemProperty("vade.spike.serverBin", nativeLibDir.resolve("spike-server").absolutePath)
    systemProperty("vade.spike.keyDir", keyDir.absolutePath)
    jvmArgs("--enable-native-access=ALL-UNNAMED")
    // Rust reads the client identity from the process environment, so it has to
    // be exported before the JVM forks. The files exist by now (spikeKeys ran).
    doFirst {
        val pub = File(keyDir, "server_static.pub.hex").readText().trim()
        val sk = File(keyDir, "client_static.sk.hex").absolutePath
        require(pub.length == 64) { "unexpected server_static.pub.hex: $pub" }
        environment("VADE_SPIKE_SERVER_PUB", pub)
        environment("VADE_SPIKE_CLIENT_SK_FILE", sk)
        environment("VADE_SPIKE_KEY_DIR", keyDir.absolutePath)
    }
}
