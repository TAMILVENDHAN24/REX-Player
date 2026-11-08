// Top-level build file where you can add configuration options common to all sub-projects/modules.
plugins {
  id("com.chaquo.python") version "17.1.0" apply false
  alias(libs.plugins.android.application) apply false
  alias(libs.plugins.kotlin.compose.compiler) apply false
  alias(libs.plugins.kotlinx.serialization) apply false
  alias(libs.plugins.ksp) apply false
  alias(libs.plugins.room) apply false
}
