// C0c spike: Miuix on Compose Multiplatform.
// No gradle wrapper in this repo: use /opt/gradle/gradle-8.10.2/bin/gradle.
pluginManagement {
    repositories {
        // Mirrors first: repo1 is reachable but slow from this host.
        maven("https://maven.aliyun.com/repository/public")
        maven("https://maven.aliyun.com/repository/google")
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositories {
        maven("https://maven.aliyun.com/repository/public")
        // androidx.navigationevent (Miuix transitive) is only on Google's maven.
        maven("https://maven.aliyun.com/repository/google")
        google()
        mavenCentral()
    }
}

rootProject.name = "vade-spike-miuix"
include(":composeApp")
