package com.blendremote.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.blendremote.client.ui.BlendRemoteTheme
import com.blendremote.client.ui.ConnectScreen
import com.blendremote.client.ui.ControlScreen

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            BlendRemoteTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    val navController = rememberNavController()
                    val vm: BlendRemoteViewModel = viewModel()

                    NavHost(
                        navController = navController,
                        startDestination = "connect",
                    ) {
                        composable("connect") {
                            ConnectScreen(
                                vm = vm,
                                onConnected = { navController.navigate("control") },
                            )
                        }
                        composable("control") {
                            ControlScreen(
                                vm = vm,
                                onDisconnect = {
                                    vm.disconnect()
                                    navController.popBackStack("connect", inclusive = false)
                                },
                            )
                        }
                    }
                }
            }
        }
    }
}