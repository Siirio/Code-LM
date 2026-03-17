package ai.engram.plugin.actions

import ai.engram.plugin.client.BackendClient
import ai.engram.plugin.client.ProjectSession
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.service
import com.intellij.openapi.progress.ProgressIndicator
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.progress.Task
import com.intellij.openapi.ui.Messages
import javax.swing.JOptionPane

class ScanProjectAction : AnAction() {

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val session = project.service<ProjectSession>()
        val client = ApplicationManager.getApplication().service<BackendClient>()

        val params = showScanModeDialog(session.rootPath) ?: return  // user cancelled

        ProgressManager.getInstance().run(object : Task.Backgroundable(project, "EngramAI: Scanning project...") {
            override fun run(indicator: ProgressIndicator) {
                indicator.isIndeterminate = true
                try {
                    val result = client.scanProject(
                        projectId = session.projectId,
                        rootPath = session.rootPath,
                        scanMode = params.mode,
                        folderPath = params.folderPath,
                        entryPoint = params.entryPoint,
                    )
                    ApplicationManager.getApplication().invokeLater {
                        Messages.showInfoMessage(
                            "Scan complete — ${result.files_found} files, ${result.classes_found} classes, ${result.functions_found} functions indexed.\n\n${result.message}",
                            "EngramAI Scan"
                        )
                    }
                } catch (ex: Exception) {
                    ApplicationManager.getApplication().invokeLater {
                        Messages.showErrorDialog(
                            "Could not reach EngramAI backend: ${ex.message}\n\nMake sure it's running: cd backend && python main.py",
                            "EngramAI Error"
                        )
                    }
                }
            }
        })
    }
}

data class ScanParams(val mode: String, val folderPath: String? = null, val entryPoint: String? = null)

/**
 * Shows a dialog asking the user which scan mode to use.
 * Returns null if the user cancels.
 */
fun showScanModeDialog(rootPath: String): ScanParams? {
    val options = arrayOf(
        "1 — Full project scan",
        "2 — Folder scan",
        "3 — Smart scan (entry point)",
    )
    val choice = JOptionPane.showOptionDialog(
        null,
        "Choose scan mode:\n\nFull scan indexes the entire project.\nFolder scan indexes only a selected subfolder.\nSmart scan follows dependency graph from a class or file.",
        "EngramAI — Choose Scan Mode",
        JOptionPane.DEFAULT_OPTION,
        JOptionPane.QUESTION_MESSAGE,
        null,
        options,
        options[0],
    )
    return when (choice) {
        0 -> ScanParams(mode = "full")
        1 -> {
            val folder = Messages.showInputDialog(
                "Which folder should I scan?\n(Relative to project root or absolute path)",
                "EngramAI — Folder Scan",
                null,
                rootPath,
                null,
            ) ?: return null
            ScanParams(mode = "folder", folderPath = folder)
        }
        2 -> {
            val entryPoint = Messages.showInputDialog(
                "Which class, file, or feature should I analyze?\n(e.g. UserController, AuthService, PaymentRepository)",
                "EngramAI — Smart Scan",
                null,
                "",
                null,
            ) ?: return null
            ScanParams(mode = "smart", entryPoint = entryPoint)
        }
        else -> null  // dialog closed / cancelled
    }
}
