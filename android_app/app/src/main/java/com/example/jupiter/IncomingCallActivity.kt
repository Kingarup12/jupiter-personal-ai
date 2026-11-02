package com.example.jupiter

import android.app.KeyguardManager
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Full-screen incoming Jupiter call (only when the OS allows full-screen notifications). Shown over the lock screen
 * with nothing private on it: "Jupiter" and the privacy-safe reason. Answer asks the user to unlock first, then opens
 * Jupiter, which joins the existing Live Conversation; nothing starts the microphone, camera or screen capture here.
 */
class IncomingCallActivity : ComponentActivity() {
    var callId: String = ""
        private set
    private var handled by mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        bind(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        bind(intent)
    }

    private fun bind(intent: Intent?) {
        val id = intent?.getStringExtra(JupiterCalls.EXTRA_CALL_ID).orEmpty()
        if (!Regex("[a-f0-9]{32}").matches(id) || JupiterCalls.accountKey(this).isEmpty()) {
            finish()
            return
        }
        callId = id
        JupiterCalls.incomingUi = java.lang.ref.WeakReference(this)
        val reason = intent?.getStringExtra(JupiterCalls.EXTRA_REASON).orEmpty().ifBlank { "Jupiter is calling" }
        val reducedMotion = runCatching {
            Settings.Global.getFloat(contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
        }.getOrDefault(false)
        setContent { IncomingCallScreen(reason, reducedMotion, handled, ::answer, ::decline) }
    }

    private fun answer() {
        if (handled) return                                     // double-tap never answers twice
        handled = true
        val open = {
            JupiterCalls.stop(this, callId)
            startActivity(Intent(this, MainActivity::class.java).setAction(JupiterCalls.ACTION_ANSWER)
                .putExtra(JupiterCalls.EXTRA_CALL_ID, callId)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            finish()
        }
        val keyguard = getSystemService(KeyguardManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && keyguard?.isKeyguardLocked == true) {
            keyguard.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() = open()
                override fun onDismissCancelled() { handled = false }   // still ringing; the user can try again
                override fun onDismissError() { handled = false }
            })
        } else {
            open()
        }
    }

    private fun decline() {
        if (handled) return
        handled = true
        JupiterCalls.declineInBackground(applicationContext, callId) {}
        finish()
    }

    override fun onDestroy() {
        if (JupiterCalls.incomingUi?.get() === this) JupiterCalls.incomingUi = null
        super.onDestroy()
    }
}

@Composable
private fun IncomingCallScreen(reason: String, reducedMotion: Boolean, handled: Boolean,
                               onAnswer: () -> Unit, onDecline: () -> Unit) {
    val pulse = if (reducedMotion) 1f else {                    // a slow, subtle "breathing" banner while ringing
        val transition = rememberInfiniteTransition(label = "ring")
        val value by transition.animateFloat(1f, 1.04f, infiniteRepeatable(tween(1800), RepeatMode.Reverse), label = "pulse")
        value
    }
    Box(Modifier.fillMaxSize().background(Color(0xFF0B1024))) {
        // The Jupiter caller banner (it already shows the Jupiter logo and name), full bleed.
        Image(
            painterResource(R.drawable.jupiter_caller_banner), contentDescription = "Jupiter",
            contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize().scale(pulse.coerceAtMost(1.04f)),
        )
        Box(Modifier.fillMaxSize().background(Brush.verticalGradient(
            0f to Color(0xB30B1024), 0.22f to Color.Transparent, 0.55f to Color.Transparent, 1f to Color(0xF20B1024))))
        Column(
            Modifier.fillMaxSize().systemBarsPadding().padding(horizontal = 24.dp, vertical = 28.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Image(painterResource(R.drawable.jupiter_caller_logo), contentDescription = null,
                    modifier = Modifier.size(28.dp).clip(RoundedCornerShape(8.dp)))
                Spacer(Modifier.width(10.dp))
                Text("Incoming Jupiter call", color = Color(0xFFDCE3FF), fontSize = 16.sp,
                    fontWeight = FontWeight.Medium, modifier = Modifier.semantics { heading() })
            }
            Spacer(Modifier.weight(1f))
            Text(reason, color = Color.White, fontSize = 19.sp, fontWeight = FontWeight.Medium, textAlign = TextAlign.Center)
            Spacer(Modifier.height(28.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                CallButton("Decline", "Decline the Jupiter call", Color(0xFFE5484D), !handled, onDecline)
                CallButton("Answer", "Answer the Jupiter call", Color(0xFF2FBF71), !handled, onAnswer)
            }
            Spacer(Modifier.height(14.dp))
            Text("Not a phone call. Answering opens Jupiter Live after you unlock.", color = Color(0xFFB4BDDD),
                fontSize = 13.sp, textAlign = TextAlign.Center)
        }
    }
}

@Composable
private fun CallButton(label: String, description: String, color: Color, enabled: Boolean, onClick: () -> Unit) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Button(
            onClick = onClick, enabled = enabled, shape = CircleShape,
            colors = ButtonDefaults.buttonColors(containerColor = color, disabledContainerColor = color.copy(alpha = 0.4f)),
            modifier = Modifier.size(76.dp).semantics { contentDescription = description },
        ) {
            Icon(painterResource(R.drawable.ic_stat_call), contentDescription = null, tint = Color.White,
                modifier = Modifier.size(30.dp).rotate(if (label == "Decline") 135f else 0f))
        }
        Spacer(Modifier.height(8.dp))
        Text(label, color = Color.White, fontSize = 15.sp)
    }
}
