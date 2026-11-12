pluginManagement {
    repositories {
        // Aliyun mirror first: maven central is reachable but slow from this box.
        maven("https://maven.aliyun.com/repository/gradle-plugin")
        gradlePluginPortal()
        mavenCentral()
    }
}

rootProject.name = "vade-kotlin-wasm"
