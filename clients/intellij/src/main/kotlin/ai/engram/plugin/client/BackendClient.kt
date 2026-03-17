package ai.engram.plugin.client

import com.google.gson.Gson
import com.intellij.openapi.components.Service
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * HTTP client that talks to the EngramAI Python backend.
 * All IDE clients (IntelliJ, VSCode, etc.) speak this same API.
 */
@Service(Service.Level.APP)
class BackendClient {

    private val baseUrl = "http://127.0.0.1:8765/api/v1"
    private val gson = Gson()
    private val json = "application/json; charset=utf-8".toMediaType()

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(300, TimeUnit.SECONDS)  // 5 min — large projects take time to scan
        .build()

    fun isBackendRunning(): Boolean {
        return try {
            val req = Request.Builder().url("http://127.0.0.1:8765/health").get().build()
            http.newCall(req).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            false
        }
    }

    fun chat(
        projectId: String,
        message: String,
        conversationId: String?,
        sessionId: String? = null,
        agentId: String? = null,
    ): ChatResponse {
        val body = mutableMapOf<String, Any?>(
            "project_id" to projectId,
            "message" to message,
            "conversation_id" to conversationId,
        )
        sessionId?.let { body["session_id"] = it }
        agentId?.let { body["agent_id"] = it }
        val response = post("/chat", body)
        return gson.fromJson(response, ChatResponse::class.java)
    }

    /**
     * Stream a chat response via SSE. Calls onChunk for each text fragment
     * and onTool when the AI invokes a tool. Blocks until the stream is complete.
     */
    fun chatStream(
        projectId: String,
        message: String,
        conversationId: String?,
        sessionId: String? = null,
        agentId: String? = null,
        onChunk: (String) -> Unit,
        onTool: (String) -> Unit,
        onAgent: (String) -> Unit = {},
        onFileEdit: (FileEditProposal) -> Unit = {},
    ) {
        val body = mutableMapOf<String, Any?>(
            "project_id" to projectId,
            "message" to message,
            "conversation_id" to conversationId,
        )
        sessionId?.let { body["session_id"] = it }
        agentId?.let { body["agent_id"] = it }
        val reqBody = gson.toJson(body).toRequestBody(json)
        val req = Request.Builder().url("$baseUrl/chat/stream").post(reqBody).build()

        http.newCall(req).execute().use { response ->
            if (!response.isSuccessful) {
                val responseBody = response.body?.string() ?: ""
                val detail = try {
                    @Suppress("UNCHECKED_CAST")
                    val map = gson.fromJson(responseBody, Map::class.java) as Map<String, Any>
                    map["detail"]?.toString() ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                throw BackendException(response.code, detail)
            }

            val reader = response.body!!.source().inputStream().bufferedReader()
            reader.forEachLine { line ->
                if (line.startsWith("data: ")) {
                    val payload = line.removePrefix("data: ").trim()
                    try {
                        @Suppress("UNCHECKED_CAST")
                        val event = gson.fromJson(payload, Map::class.java) as Map<String, Any>
                        when {
                            event.containsKey("chunk") -> onChunk(event["chunk"] as String)
                            event.containsKey("tool") -> onTool(event["tool"] as String)
                            event.containsKey("agent") -> onAgent(event["agent"] as String)
                            event.containsKey("file_edit") -> {
                                @Suppress("UNCHECKED_CAST")
                                val edit = event["file_edit"] as Map<String, Any>
                                onFileEdit(FileEditProposal(
                                    filePath = edit["file_path"] as? String ?: "",
                                    description = edit["description"] as? String ?: "",
                                    originalSnippet = edit["original_snippet"] as? String ?: "",
                                    newSnippet = edit["new_snippet"] as? String ?: "",
                                ))
                            }
                            event["done"] == true -> { /* stream complete */ }
                        }
                    } catch (_: Exception) {
                        // Skip malformed SSE lines
                    }
                }
            }
        }
    }

    fun scanProject(
        projectId: String,
        rootPath: String,
        branch: String = "main",
        scanMode: String = "full",
        folderPath: String? = null,
        entryPoint: String? = null,
    ): ScanResponse {
        val body = mutableMapOf<String, Any?>(
            "project_id" to projectId,
            "root_path" to rootPath,
            "branch" to branch,
            "scan_mode" to scanMode,
        )
        folderPath?.let { body["folder_path"] = it }
        entryPoint?.let { body["entry_point"] = it }
        val response = post("/projects/scan", body)
        return gson.fromJson(response, ScanResponse::class.java)
    }

    fun getProjectStatus(projectId: String): ProjectStatus {
        val req = Request.Builder().url("$baseUrl/projects/$projectId/status").get().build()
        val body = http.newCall(req).execute().use { response ->
            if (!response.isSuccessful) throw BackendException(response.code, "status check failed")
            response.body!!.string()
        }
        return gson.fromJson(body, ProjectStatus::class.java)
    }

    /** Create a new chat session, optionally bound to an agent. */
    fun createSession(projectId: String, agentId: String? = null): CreateSessionResponse {
        val body = mutableMapOf<String, Any?>("project_id" to projectId)
        agentId?.let { body["agent_id"] = it }
        val response = post("/sessions", body)
        return gson.fromJson(response, CreateSessionResponse::class.java)
    }

    /** List all chat sessions for a project, most recent first. */
    fun listSessions(projectId: String): List<ChatSession> {
        val req = Request.Builder().url("$baseUrl/projects/$projectId/sessions").get().build()
        val body = http.newCall(req).execute().use { response ->
            if (!response.isSuccessful) throw BackendException(response.code, "failed to list sessions")
            response.body!!.string()
        }
        return gson.fromJson(body, Array<ChatSession>::class.java).toList()
    }

    /** Retrieve messages for a given session. */
    fun getMessages(sessionId: String): List<SessionMessage> {
        val req = Request.Builder().url("$baseUrl/sessions/$sessionId/messages").get().build()
        val body = http.newCall(req).execute().use { response ->
            if (!response.isSuccessful) throw BackendException(response.code, "failed to get messages")
            response.body!!.string()
        }
        return gson.fromJson(body, Array<SessionMessage>::class.java).toList()
    }

    /** Delete a chat session and all its messages. */
    fun deleteSession(sessionId: String) {
        val req = Request.Builder()
            .url("$baseUrl/sessions/$sessionId")
            .delete()
            .build()
        http.newCall(req).execute().use { response ->
            if (!response.isSuccessful && response.code != 404) {
                throw BackendException(response.code, "failed to delete session")
            }
        }
    }

    /** List available agent personas for a project. */
    fun listAgents(projectId: String): List<AgentPersona> {
        val req = Request.Builder().url("$baseUrl/projects/$projectId/agents").get().build()
        val body = http.newCall(req).execute().use { response ->
            if (!response.isSuccessful) throw BackendException(response.code, "failed to list agents")
            response.body!!.string()
        }
        return gson.fromJson(body, Array<AgentPersona>::class.java).toList()
    }

    /** Create a custom agent persona. */
    fun createAgent(projectId: String, name: String, description: String, systemPromptExtra: String): AgentPersona {
        val body = mapOf(
            "project_id" to projectId,
            "name" to name,
            "description" to description,
            "system_prompt_extra" to systemPromptExtra,
        )
        val response = post("/agents", body)
        return gson.fromJson(response, AgentPersona::class.java)
    }

    private fun post(path: String, body: Any): String {
        val reqBody = gson.toJson(body).toRequestBody(json)
        val req = Request.Builder().url("$baseUrl$path").post(reqBody).build()
        return http.newCall(req).execute().use { response ->
            val responseBody = response.body!!.string()
            if (!response.isSuccessful) {
                // Extract the human-readable "detail" field that FastAPI sets on errors,
                // falling back to the raw body so the user always sees a meaningful message.
                val detail = try {
                    @Suppress("UNCHECKED_CAST")
                    val map = gson.fromJson(responseBody, Map::class.java) as Map<String, Any>
                    map["detail"]?.toString() ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                throw BackendException(response.code, detail)
            }
            responseBody
        }
    }
}

/** Thrown when the backend returns a non-2xx status code. */
class BackendException(val statusCode: Int, message: String) : Exception(message)

data class ChatResponse(
    val reply: String,
    val conversation_id: String,
    val memory_update_proposed: Boolean = false,
    val memory_update_proposal: Map<String, Any>? = null,
)

data class ScanResponse(
    val project_id: String,
    val status: String,
    val files_found: Int = 0,
    val classes_found: Int = 0,
    val functions_found: Int = 0,
    val modules: List<String> = emptyList(),
    val message: String = "",
)

data class ProjectStatus(
    val project_id: String,
    val indexed: Boolean,
)

/** Represents a chat session returned by the backend. */
data class ChatSession(
    val id: String,
    val title: String?,
    val created_at: String,
    val message_count: Int = 0,
) {
    /** Display label used in the sessions combo box. */
    override fun toString(): String = title ?: "Chat ${id.take(8)}..."
}

/** Represents an agent persona returned by the backend. */
data class AgentPersona(
    val id: String,
    val name: String,
    val description: String?,
) {
    override fun toString(): String = name
}

/** Response from creating a new session. */
data class CreateSessionResponse(
    val id: String,
    val project_id: String,
)

/** A single message in a session's history. */
data class SessionMessage(
    val role: String,
    val content: String,
)

/** A proposed file edit sent from the backend for user approval. */
data class FileEditProposal(
    val filePath: String,
    val description: String,
    val originalSnippet: String,
    val newSnippet: String,
)
