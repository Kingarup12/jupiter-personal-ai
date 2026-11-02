import java.util.Properties

plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.kotlin.serialization)
}

android {
    namespace = "com.example.jupiter"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.arupbairagya.jupiter"
        minSdk = 24
        targetSdk = 36
        versionCode = 85
        versionName = "1.0.84"
        resValue(
            "string",
            "gmail_server_client_id",
            providers.environmentVariable("JUPITER_GMAIL_SERVER_CLIENT_ID").orElse("").get(),
        )
        // Jupiter calls (marker 172): the Firebase project's PUBLIC app identifiers, the same values the
        // google-services plugin would generate. Empty -> Firebase stays uninitialised and push is unavailable
        // (an open Jupiter app still rings). The server-side FCM credential never goes into the APK.
        mapOf(
            "google_app_id" to "JUPITER_FCM_APP_ID",
            "gcm_defaultSenderId" to "JUPITER_FCM_SENDER_ID",
            "google_api_key" to "JUPITER_FCM_API_KEY",
            "project_id" to "JUPITER_FCM_PROJECT_ID",
        ).forEach { (name, variable) ->
            resValue("string", name, providers.environmentVariable(variable).orElse("").get())
        }
    }

    signingConfigs {
        create("release") {
            val propsFile = rootProject.file("keystore.properties")
            if (propsFile.exists()) {
                val props = Properties()
                props.load(propsFile.inputStream())
                storeFile = rootProject.file(props["storeFile"] as String)
                storePassword = props["storePassword"] as String
                keyAlias = props["keyAlias"] as String
                keyPassword = props["keyPassword"] as String
                // Same Jupiter release key as every previous release. v1 (legacy JAR)
                // signing is not needed at minSdk 24; v2 + v3 is what current Android
                // verifies, and shipping v3 avoids looking like a repackaged APK.
                enableV1Signing = false
                enableV2Signing = true
                enableV3Signing = true
            }
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".test.debug"
        }
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
      compose = true
      resValues = true
      aidl = false
      buildConfig = true
      shaders = false
    }

    packaging {
      resources {
        excludes += "/META-INF/{AL2.0,LGPL2.1}"
      }
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
  val composeBom = platform(libs.androidx.compose.bom)
  implementation(composeBom)
  androidTestImplementation(composeBom)

  // Core Android dependencies
  implementation(libs.androidx.core.ktx)
  implementation(libs.androidx.lifecycle.runtime.ktx)
  implementation(libs.androidx.activity.compose)
  implementation("com.google.android.gms:play-services-auth:21.6.0")
  // Free push for incoming Jupiter calls (marker 172); no paid Firebase product is used.
  implementation("com.google.firebase:firebase-messaging:24.1.0")

  // Arch Components
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)

  // Compose
  implementation(libs.androidx.compose.ui)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.androidx.compose.material3)
  // Tooling
  debugImplementation(libs.androidx.compose.ui.tooling)
  // Instrumented tests
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  debugImplementation(libs.androidx.compose.ui.test.manifest)

  // Local tests: jUnit, coroutines, Android runner
  testImplementation(libs.junit)
  testImplementation(libs.kotlinx.coroutines.test)

  // Instrumented tests: jUnit rules and runners
  androidTestImplementation(libs.androidx.test.core)
  androidTestImplementation(libs.androidx.test.ext.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.androidx.test.espresso.core)

  // Navigation
  implementation(libs.androidx.navigation3.ui)
  implementation(libs.androidx.navigation3.runtime)
  implementation(libs.androidx.lifecycle.viewmodel.navigation3)
}

// Marker 186: a RELEASE build must never ship without Firebase's public app identifiers. 1.0.72 did (the variables were
// unset on the build machine): Firebase stayed uninitialised, so closed-app call ringing and message alerts silently
// stopped. Values are read at configuration time (configuration-cache safe); debug builds are unaffected. A deliberate
// exception needs -PjupiterAllowNoFirebase=true.
val missingFirebaseIdentifiers = listOf("JUPITER_FCM_APP_ID", "JUPITER_FCM_SENDER_ID", "JUPITER_FCM_API_KEY", "JUPITER_FCM_PROJECT_ID")
    .filter { providers.environmentVariable(it).orElse("").get().isBlank() }
val allowReleaseWithoutFirebase = providers.gradleProperty("jupiterAllowNoFirebase").orElse("false").get() == "true"
tasks.matching { it.name == "preReleaseBuild" }.configureEach {             // first task of any release build
    val missing = missingFirebaseIdentifiers
    val allowed = allowReleaseWithoutFirebase
    doFirst {
        if (missing.isNotEmpty() && !allowed) {
            throw GradleException(
                "Release build stopped: Firebase identifiers missing (" + missing.joinToString() + "). Without them push " +
                    "(closed-app calls, message alerts) is disabled. Set them, or pass -PjupiterAllowNoFirebase=true deliberately."
            )
        }
    }
}
