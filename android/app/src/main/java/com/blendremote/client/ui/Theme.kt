package com.blendremote.client.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// Blender 品牌橙 + 深色工作台配色
private val BrandOrange = Color(0xFFE87D0D)
private val BrandOrangeDark = Color(0xFFB9640A)

private val DarkColors = darkColorScheme(
    primary = BrandOrange,
    onPrimary = Color.White,
    primaryContainer = Color(0xFF5A3204),
    onPrimaryContainer = Color.White,
    secondary = Color(0xFF9A9A9A),
    background = Color(0xFF1A1A1D),
    onBackground = Color(0xFFE5E5E5),
    surface = Color(0xFF232327),
    onSurface = Color(0xFFE5E5E5),
    surfaceVariant = Color(0xFF2C2C31),
    onSurfaceVariant = Color(0xFFA6A6AD),
    outline = Color(0xFF3A3A40),
    error = Color(0xFFEF4444),
)

private val LightColors = lightColorScheme(
    primary = BrandOrangeDark,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFFFE7CC),
    onPrimaryContainer = Color(0xFF4A2800),
    secondary = BrandOrange,
    background = Color(0xFFF6F6F7),
    onBackground = Color(0xFF171717),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF171717),
    surfaceVariant = Color(0xFFEFEFF2),
    onSurfaceVariant = Color(0xFF52525B),
    outline = Color(0xFFD3D4DA),
    error = Color(0xFFDC2626),
)

@Composable
fun BlendRemoteTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        content = content,
    )
}