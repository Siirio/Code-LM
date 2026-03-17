package ai.engram.plugin.client

import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import java.util.UUID

/**
 * Holds per-project state: project ID, active conversation ID.
 * One instance per open IntelliJ project.
 */
@Service(Service.Level.PROJECT)
class ProjectSession(private val project: Project) {

    // Stable ID derived from project path — same project always gets same ID
    val projectId: String = UUID.nameUUIDFromBytes(
        (project.basePath ?: project.name).toByteArray()
    ).toString()

    var conversationId: String? = null

    val rootPath: String get() = project.basePath ?: ""
}
