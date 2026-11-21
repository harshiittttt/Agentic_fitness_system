package expo.modules.pcmaudio

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.util.Base64
import expo.modules.interfaces.permissions.Permissions
import expo.modules.kotlin.Promise
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Raw microphone + speaker for the live voice agent. PCM16 mono 16 kHz both ways, carried to JavaScript as base64 TEXT
 * (a byte array inside an event is what the previous library lost). Every failure is returned or emitted as readable text.
 *
 *   start()   -> "" when the microphone and speaker run, otherwise the reason
 *   stop()    -> releases everything and gives the phone's normal audio mode back
 *   play(b64) -> queue speech for the speaker;  flush() -> drop what is queued/playing at once (interrupting)
 *   status()  -> "recording=1;frames=N;aec=1;ns=1;playing=0;error=..."
 *   event "mic" {b64}, event "error" {message}
 */
class PcmAudioModule : Module() {
    private val sampleRate = 16000
    private var record: AudioRecord? = null
    private var track: AudioTrack? = null
    private var aec: AcousticEchoCanceler? = null
    private var ns: NoiseSuppressor? = null
    private var micThread: Thread? = null
    private var playThread: Thread? = null
    private val playQueue = LinkedBlockingQueue<ByteArray>()
    private var focus: AudioFocusRequest? = null
    private var previousMode = AudioManager.MODE_NORMAL
    private var previousSpeaker = false

    @Volatile private var recording = false
    @Volatile private var playerAlive = false
    @Volatile private var frames = 0
    @Volatile private var lastError = ""

    override fun definition() = ModuleDefinition {
        Name("PcmAudio")
        Events("mic", "error")

        AsyncFunction("getPermission") { promise: Promise ->
            Permissions.getPermissionsWithPermissionsManager(appContext.permissions, promise, android.Manifest.permission.RECORD_AUDIO)
        }

        AsyncFunction("requestPermission") { promise: Promise ->
            Permissions.askForPermissionsWithPermissionsManager(appContext.permissions, promise, android.Manifest.permission.RECORD_AUDIO)
        }

        Function("start") {
            startAudio()
        }

        Function("stop") {
            stopAudio()
            ""
        }

        Function("play") { b64: String ->
            try {
                playQueue.add(Base64.decode(b64, Base64.DEFAULT))
            } catch (e: Exception) {
                lastError = "play: " + (e.message ?: e.javaClass.simpleName)
            }
            ""
        }

        Function("flush") {
            flushPlayback()
            ""
        }

        Function("status") {
            "recording=" + (if (recording) 1 else 0) + ";frames=" + frames + ";aec=" + (if (aec?.enabled == true) 1 else 0) +
                ";ns=" + (if (ns?.enabled == true) 1 else 0) + ";queued=" + playQueue.size + ";error=" + lastError
        }
    }

    private fun audioManager(): AudioManager? {
        val ctx = appContext.reactContext ?: return null
        return ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    }

    private fun startAudio(): String {
        if (recording) return ""
        lastError = ""
        frames = 0
        val am = audioManager() ?: return "no application context"
        try {
            previousMode = am.mode
            previousSpeaker = am.isSpeakerphoneOn
            am.mode = AudioManager.MODE_IN_COMMUNICATION
            am.isSpeakerphoneOn = true
            requestFocusBestEffort(am)

            val minRecord = AudioRecord.getMinBufferSize(sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            if (minRecord <= 0) {
                restoreAudio(am)
                return "this phone does not support 16 kHz recording (size $minRecord)"
            }
            val rec = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minRecord, sampleRate / 5 * 2)
            )
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                rec.release()
                restoreAudio(am)
                return "the microphone could not be opened (permission refused, or another app is using it)"
            }
            try {
                if (AcousticEchoCanceler.isAvailable()) {
                    aec = AcousticEchoCanceler.create(rec.audioSessionId)
                    aec?.enabled = true
                }
            } catch (e: Exception) {
                lastError = "echo canceller: " + (e.message ?: "")
            }
            try {
                if (NoiseSuppressor.isAvailable()) {
                    ns = NoiseSuppressor.create(rec.audioSessionId)
                    ns?.enabled = true
                }
            } catch (e: Exception) {
                lastError = "noise suppressor: " + (e.message ?: "")
            }
            rec.startRecording()
            if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                rec.release()
                restoreAudio(am)
                return "the microphone did not start recording"
            }
            record = rec

            val minTrack = AudioTrack.getMinBufferSize(sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val tr = AudioTrack(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build(),
                maxOf(minTrack, sampleRate / 5 * 2),
                AudioTrack.MODE_STREAM,
                am.generateAudioSessionId()
            )
            tr.play()
            track = tr

            recording = true
            playerAlive = true
            micThread = Thread {
                val buf = ByteArray(640)   // 20 ms
                while (recording) {
                    val n = try { rec.read(buf, 0, buf.size) } catch (e: Exception) { -1 }
                    if (n > 0) {
                        frames++
                        try {
                            sendEvent("mic", mapOf("b64" to Base64.encodeToString(buf, 0, n, Base64.NO_WRAP)))
                        } catch (e: Exception) {
                            lastError = "mic event: " + (e.message ?: e.javaClass.simpleName)
                        }
                    } else if (n < 0) {
                        lastError = "microphone read failed ($n)"
                        try { sendEvent("error", mapOf("message" to lastError)) } catch (e: Exception) { /* nothing more to do */ }
                        break
                    }
                }
            }.also { it.isDaemon = true; it.start() }
            playThread = Thread {
                while (playerAlive) {
                    val chunk = try { playQueue.poll(100, TimeUnit.MILLISECONDS) } catch (e: InterruptedException) { null }
                    if (chunk != null) {
                        try { tr.write(chunk, 0, chunk.size) } catch (e: Exception) { lastError = "speaker: " + (e.message ?: "") }
                    }
                }
            }.also { it.isDaemon = true; it.start() }
            return ""
        } catch (e: Exception) {
            val why = e.javaClass.simpleName + ": " + (e.message ?: "")
            lastError = why
            stopAudio()
            return why
        }
    }

    private fun requestFocusBestEffort(am: AudioManager) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build()
                    )
                    .build()
                am.requestAudioFocus(req)          // the answer is ignored on purpose: not getting focus must not stop the microphone
                focus = req
            }
        } catch (e: Exception) {
            lastError = "audio focus: " + (e.message ?: "")
        }
    }

    private fun flushPlayback() {
        playQueue.clear()
        val tr = track ?: return
        try {
            tr.pause()
            tr.flush()
            tr.play()
        } catch (e: Exception) {
            lastError = "flush: " + (e.message ?: "")
        }
    }

    private fun stopAudio() {
        recording = false
        playerAlive = false
        playQueue.clear()
        try { micThread?.join(400) } catch (e: Exception) { /* give up waiting */ }
        try { playThread?.join(400) } catch (e: Exception) { /* give up waiting */ }
        micThread = null
        playThread = null
        try { record?.stop() } catch (e: Exception) { /* not recording */ }
        try { record?.release() } catch (e: Exception) { /* already released */ }
        record = null
        try { aec?.release() } catch (e: Exception) { /* ignore */ }
        try { ns?.release() } catch (e: Exception) { /* ignore */ }
        aec = null
        ns = null
        try { track?.pause(); track?.flush(); track?.stop() } catch (e: Exception) { /* not playing */ }
        try { track?.release() } catch (e: Exception) { /* already released */ }
        track = null
        val am = audioManager()
        if (am != null) restoreAudio(am)
    }

    private fun restoreAudio(am: AudioManager) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                focus?.let { am.abandonAudioFocusRequest(it) }
            }
            focus = null
            am.isSpeakerphoneOn = previousSpeaker
            am.mode = previousMode
        } catch (e: Exception) {
            lastError = "restore: " + (e.message ?: "")
        }
    }
}
