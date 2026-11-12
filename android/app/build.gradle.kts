import java.security.MessageDigest
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}
val serverCmake = rootProject.file("../server/CMakeLists.txt").readText()
val serverVersion = Regex("project\\(hardcharger VERSION ([0-9.]+)").find(serverCmake)!!.groupValues[1]
val distribution = providers.gradleProperty("hardchargerDistribution").orElse("local").get()
require(distribution in listOf("local", "play")) { "hardchargerDistribution must be local or play" }
val nativeDirectory = "src/${if (distribution == "play") "play" else "main"}/jniLibs"
val nativeAbis = listOf("arm64-v8a", "x86_64").filter { file("$nativeDirectory/$it/libhardcharger_android.so").isFile }
// Preparing Play artifacts must not replace the APK used by the local emulator launcher.
if (distribution == "play") layout.buildDirectory.set(rootProject.layout.projectDirectory.dir("build/app-play"))
android {
    namespace = "com.gizmodata.hardcharger"
    compileSdk = 36
    ndkVersion = "28.2.13676358"
    defaultConfig {
        applicationId = "com.gizmodata.hardcharger"
        minSdk = 28
        targetSdk = 36
        versionCode = providers.environmentVariable("HARDCHARGER_ANDROID_BUILD_NUMBER").orNull?.toInt() ?: 1
        versionName = serverVersion
        buildConfigField("String", "DISTRIBUTION", "\"$distribution\"")
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        ndk { abiFilters += nativeAbis.ifEmpty { listOf("arm64-v8a") } }
    }
    buildFeatures { compose = true; buildConfig = true }
    sourceSets.getByName("main").jniLibs.setSrcDirs(listOf(nativeDirectory))
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    packaging { jniLibs { useLegacyPackaging = false } }
    signingConfigs {
        val path = providers.environmentVariable("HARDCHARGER_ANDROID_KEYSTORE").orNull
        if (path != null) create("release") {
            storeFile = file(path)
            storePassword = providers.environmentVariable("HARDCHARGER_ANDROID_STORE_PASSWORD").get()
            keyAlias = providers.environmentVariable("HARDCHARGER_ANDROID_KEY_ALIAS").get()
            keyPassword = providers.environmentVariable("HARDCHARGER_ANDROID_KEY_PASSWORD").get()
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release")
        }
    }
}
kotlin { compilerOptions { jvmTarget.set(JvmTarget.JVM_17) } }
dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.10.01")
    implementation(composeBom)
    androidTestImplementation(composeBom)
    implementation("androidx.activity:activity-compose:1.11.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
    implementation("androidx.lifecycle:lifecycle-process:2.9.4")
    implementation("androidx.webkit:webkit:1.14.0")
    implementation("androidx.browser:browser:1.9.0")
    implementation("androidx.core:core-ktx:1.17.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.zxing:core:3.5.3")
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.7.0")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}

// Never produce an installable APK that would fail to load its server on launch.
val verifyNative by tasks.registering {
    doLast {
        check(nativeAbis.isNotEmpty()) {
            "Build the native server first: android/scripts/build-native.sh arm64-v8a $distribution"
        }
        nativeAbis.forEach { abi ->
            val metadata = file("$nativeDirectory/$abi.json")
            check(metadata.isFile) { "Native build metadata missing; rebuild $abi for $distribution" }
            val info = groovy.json.JsonSlurper().parse(metadata) as Map<*, *>
            check(info["distribution"] == distribution) { "Wrong native distribution for $abi" }
            val digest = MessageDigest.getInstance("SHA-256")
            file("$nativeDirectory/$abi/libhardcharger_android.so").inputStream().use { input ->
                val buffer = ByteArray(65536)
                var count = input.read(buffer)
                while (count != -1) { digest.update(buffer, 0, count); count = input.read(buffer) }
            }
            check(info["sha256"] == digest.digest().joinToString("") { "%02x".format(it) }) {
                "Native library does not match its build metadata: $abi"
            }
        }
    }
}
tasks.configureEach {
    if (name.startsWith("merge") && name.endsWith("NativeLibs") && !name.contains("AndroidTest")) dependsOn(verifyNative)
}
