package ai.engram.plugin.toolwindow

import ai.engram.plugin.actions.showScanModeDialog
import ai.engram.plugin.client.*
import java.io.File
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.content.ContentFactory
import java.awt.BorderLayout
import java.awt.FlowLayout
import java.awt.event.KeyEvent
import java.awt.event.KeyListener
import javax.swing.*

/**
 * Creates the CodeLM tool window in the IDE sidebar.
 * Delegates all content to [EngramChatPanel].
 */
class EngramToolWindowFactory : ToolWindowFactory {

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val panel = EngramChatPanel(project)
        val content = ContentFactory.getInstance().createContent(panel, "", false)
        toolWindow.contentManager.addContent(content)
    }
}

/**
 * Main chat panel with multi-session tabs and agent selector.
 *
 * Layout:
 *   Toolbar  — [+ New Chat] [Sessions dropdown] Agent: [Agent dropdown] [+ Agent]
 *   Center   — scrollable chat area
 *   Bottom   — text input + Send button
 */
class EngramChatPanel(private val project: Project) : JPanel(BorderLayout()) {

    companion object {
        val HELP_TEXT = """
CodeLM — AI Software Architect in your IDE.

Scan modes (type in chat or use Tools → Scan Project):
  /full-scan         — index the entire project
  /auto-scan <hint>  — (default) AI finds context automatically
  /package-scan      — scan the package of your currently open file

Agents auto-assigned per message:
  [debugger]   — bugs, errors, root-cause analysis
  [codegen]    — implement, add, create new code
  [architect]  — design, structure, dependencies, DRY

Each agent runs with a fresh context. File edits require your approval.

Type a message below and press Enter.

""".trimIndent()
    }


    private val chatArea = JTextArea().apply {
        isEditable = false
        lineWrap = true
        wrapStyleWord = true
        font = java.awt.Font("JetBrains Mono", java.awt.Font.PLAIN, 13)
        text = HELP_TEXT
    }

    private val inputField = JTextField().apply {
        toolTipText = "Ask about architecture, request code, query the graph..."
    }

    private val sendButton = JButton("Send")

    private val client = ApplicationManager.getApplication().service<BackendClient>()
    private val session = project.service<ProjectSession>()

    // ── Session / agent state ──────────────────────────────────────────────

    private var currentSessionId: String? = null
    private var currentAgentId: String? = null
    private val projectId: String get() = session.projectId

    // ── Toolbar components ─────────────────────────────────────────────────

    private val newChatButton = JButton("+ New Chat")
    private val deleteChatButton = JButton("🗑").apply { toolTipText = "Delete this chat" }
    private val sessionsCombo = JComboBox<ChatSession>()
    private val agentsCombo = JComboBox<AgentPersona>()
    private val newAgentButton = JButton("+ Agent")

    /** Sentinel entry for the "Default" agent (no custom persona). */
    private val defaultAgent = AgentPersona(id = "", name = "Default", description = null)

    // Spinner frames cycling during scan
    private val spinnerFrames = listOf("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    private var spinnerIndex = 0
    private var scanTimer: Timer? = null
    private var scanLineStart = 0  // document offset where the animated line begins

    /** Guard to suppress combo-box listener while we are programmatically updating items. */
    private var suppressSessionSwitch = false

    /** When true, all file edit proposals in this conversation are auto-accepted. */
    private var acceptAllEdits = false

    init {
        // ── Toolbar ────────────────────────────────────────────────────────
        val toolbar = JPanel(FlowLayout(FlowLayout.LEFT, 4, 2))
        toolbar.add(newChatButton)
        toolbar.add(deleteChatButton)
        toolbar.add(sessionsCombo)
        toolbar.add(JLabel("Agent:"))
        toolbar.add(agentsCombo)
        toolbar.add(newAgentButton)

        sessionsCombo.maximumRowCount = 15
        agentsCombo.maximumRowCount = 10

        add(toolbar, BorderLayout.NORTH)
        add(JBScrollPane(chatArea), BorderLayout.CENTER)

        // Bottom: input + send button
        val bottomPanel = JPanel(BorderLayout())
        bottomPanel.add(inputField, BorderLayout.CENTER)
        bottomPanel.add(sendButton, BorderLayout.EAST)
        add(bottomPanel, BorderLayout.SOUTH)

        // ── Listeners ──────────────────────────────────────────────────────

        inputField.addKeyListener(object : KeyListener {
            override fun keyTyped(e: KeyEvent) {}
            override fun keyReleased(e: KeyEvent) {}
            override fun keyPressed(e: KeyEvent) {
                if (e.keyCode == KeyEvent.VK_ENTER) sendMessage()
            }
        })

        sendButton.addActionListener { sendMessage() }

        newChatButton.addActionListener { createNewChat() }
        deleteChatButton.addActionListener { deleteCurrentChat() }

        sessionsCombo.addActionListener {
            if (suppressSessionSwitch) return@addActionListener
            val selected = sessionsCombo.selectedItem as? ChatSession ?: return@addActionListener
            if (selected.id != currentSessionId) {
                currentSessionId = selected.id
                loadSessionMessages(selected.id)
            }
        }

        agentsCombo.addActionListener {
            val selected = agentsCombo.selectedItem as? AgentPersona ?: return@addActionListener
            currentAgentId = selected.id.ifEmpty { null }
        }

        newAgentButton.addActionListener { showCreateAgentDialog() }

        // ── Kick off startup ───────────────────────────────────────────────
        checkBackendStatus()
    }

    // ── Scan animation ────────────────────────────────────────────────────

    private fun startScanAnimation(label: String) {
        SwingUtilities.invokeLater {
            scanLineStart = chatArea.document.length
            chatArea.append("${spinnerFrames[0]} $label\n")
            chatArea.caretPosition = chatArea.document.length
        }
        scanTimer = Timer(80) {
            spinnerIndex = (spinnerIndex + 1) % spinnerFrames.size
            SwingUtilities.invokeLater { updateScanLine(label) }
        }
        scanTimer?.start()
    }

    private fun updateScanLine(label: String) {
        val doc = chatArea.document
        val currentEnd = doc.length
        val lineLen = currentEnd - scanLineStart
        if (lineLen > 0) {
            doc.remove(scanLineStart, lineLen)
        }
        doc.insertString(scanLineStart, "${spinnerFrames[spinnerIndex]} $label\n", null)
        chatArea.caretPosition = doc.length
    }

    private fun stopScanAnimation(finalLine: String) {
        scanTimer?.stop()
        scanTimer = null
        SwingUtilities.invokeLater {
            val doc = chatArea.document
            val lineLen = doc.length - scanLineStart
            if (lineLen > 0) doc.remove(scanLineStart, lineLen)
            doc.insertString(scanLineStart, "$finalLine\n\n", null)
            chatArea.caretPosition = doc.length
        }
    }

    // ── Scan flow ─────────────────────────────────────────────────────────

    /**
     * Shows the scan-mode dialog on the EDT and, if the user confirms,
     * runs the scan on a background thread with an animated status line.
     */
    private fun promptAndScan() {
        val params = showScanModeDialog(session.rootPath) ?: run {
            appendToChat("Scan skipped. You can trigger it later via Tools → Scan Project.\n\n")
            return
        }
        val label = when (params.mode) {
            "folder" -> "Scanning folder: ${params.folderPath}..."
            "smart"  -> "Smart scanning from: ${params.entryPoint}..."
            else     -> "Scanning project files..."
        }
        startScanAnimation(label)
        Thread {
            try {
                val result = client.scanProject(
                    projectId = projectId,
                    rootPath = session.rootPath,
                    scanMode = params.mode,
                    folderPath = params.folderPath,
                    entryPoint = params.entryPoint,
                )
                stopScanAnimation("✓ Scan complete — ${result.files_found} files, ${result.classes_found} classes indexed")
            } catch (e: Exception) {
                stopScanAnimation("⚠ Scan failed: ${e.message ?: "Unknown error"}. Retry via Tools → Scan Project.")
            }
        }.start()
    }

    // ── Startup flow ──────────────────────────────────────────────────────

    private fun checkBackendStatus() {
        Thread {
            val running = client.isBackendRunning()
            if (!running) {
                SwingUtilities.invokeLater {
                    appendToChat("⚠ Backend not running. Start it with:\n  cd backend && python main.py\n\n")
                }
                return@Thread
            }

            try {
                val status = client.getProjectStatus(projectId)
                if (status.indexed) {
                    SwingUtilities.invokeLater {
                        appendToChat("✓ Backend connected. Project indexed — ready.\n\n")
                    }
                } else {
                    SwingUtilities.invokeLater {
                        appendToChat("✓ Backend connected. Project not indexed yet.\n  Use /full-scan, /auto-scan, or /package-scan when ready.\n\n")
                    }
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    appendToChat("✓ Backend connected.\n⚠ Could not check index status: ${e.message ?: "Unknown error"}\n\n")
                }
            }

            // Load sessions and agents after backend check
            loadSessionsAndAgents()
        }.start()
    }

    // ── Session / agent loading ───────────────────────────────────────────

    private fun loadSessionsAndAgents() {
        try {
            val sessions = client.listSessions(projectId)
            val agents = client.listAgents(projectId)

            SwingUtilities.invokeLater {
                // Populate agents combo
                agentsCombo.removeAllItems()
                agentsCombo.addItem(defaultAgent)
                agents.forEach { agentsCombo.addItem(it) }

                // Populate sessions combo
                populateSessionsCombo(sessions)

                if (sessions.isNotEmpty()) {
                    // Auto-select most recent session (first in list — backend returns most recent first)
                    val mostRecent = sessions.first()
                    currentSessionId = mostRecent.id
                    sessionsCombo.selectedItem = mostRecent
                    loadSessionMessages(mostRecent.id)
                } else {
                    // No sessions exist — create one
                    createNewChat()
                }
            }
        } catch (e: Exception) {
            SwingUtilities.invokeLater {
                appendToChat("⚠ Could not load sessions/agents: ${e.message ?: "Unknown error"}\n\n")
                // Still usable — create a session on demand
            }
        }
    }

    private fun populateSessionsCombo(sessions: List<ChatSession>) {
        suppressSessionSwitch = true
        val previousSelection = currentSessionId
        sessionsCombo.removeAllItems()
        sessions.forEach { sessionsCombo.addItem(it) }
        // Re-select the previously active session if it still exists
        sessions.find { it.id == previousSelection }?.let { sessionsCombo.selectedItem = it }
        suppressSessionSwitch = false
    }

    private fun refreshSessionsCombo() {
        Thread {
            try {
                val sessions = client.listSessions(projectId)
                SwingUtilities.invokeLater { populateSessionsCombo(sessions) }
            } catch (_: Exception) {
                // Silently ignore — non-critical refresh
            }
        }.start()
    }

    private fun loadSessionMessages(sessionId: String) {
        Thread {
            try {
                val messages = client.getMessages(sessionId)
                SwingUtilities.invokeLater {
                    chatArea.text = ""
                    if (messages.isEmpty()) {
                        appendToChat(HELP_TEXT + "\n")
                    } else {
                        messages.forEach { msg ->
                            val prefix = if (msg.role == "user") "You" else "CodeLM"
                            appendToChat("$prefix: ${msg.content}\n\n")
                        }
                    }
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    appendToChat("⚠ Could not load messages: ${e.message ?: "Unknown error"}\n\n")
                }
            }
        }.start()
    }

    // ── New chat / new agent ──────────────────────────────────────────────

    private fun createNewChat() {
        Thread {
            try {
                val created = client.createSession(projectId, currentAgentId)
                SwingUtilities.invokeLater {
                    currentSessionId = created.id
                    acceptAllEdits = false
                    chatArea.text = HELP_TEXT
                    refreshSessionsCombo()
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    appendToChat("⚠ Could not create session: ${e.message ?: "Unknown error"}\n\n")
                }
            }
        }.start()
    }

    private fun deleteCurrentChat() {
        val sessionId = currentSessionId ?: run {
            Messages.showInfoMessage("No active chat to delete.", "CodeLM")
            return
        }
        val confirm = Messages.showYesNoDialog(
            "Delete this chat and all its messages?\nThis cannot be undone.",
            "Delete Chat",
            Messages.getWarningIcon(),
        )
        if (confirm != Messages.YES) return

        Thread {
            try {
                client.deleteSession(sessionId)
                SwingUtilities.invokeLater {
                    currentSessionId = null
                    acceptAllEdits = false
                    chatArea.text = HELP_TEXT
                    refreshSessionsCombo()
                    // If there are other sessions, the combo will reselect one;
                    // otherwise create a fresh session automatically.
                    if (sessionsCombo.itemCount == 0) createNewChat()
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    appendToChat("⚠ Could not delete chat: ${e.message ?: "Unknown error"}\n\n")
                }
            }
        }.start()
    }

    private fun showCreateAgentDialog() {
        val nameField = JTextField(20)
        val descField = JTextField(30)
        val promptField = JTextArea(4, 30).apply {
            lineWrap = true
            wrapStyleWord = true
        }

        val panel = JPanel().apply {
            layout = BoxLayout(this, BoxLayout.Y_AXIS)
            add(JLabel("Name:"))
            add(nameField)
            add(Box.createVerticalStrut(6))
            add(JLabel("Description:"))
            add(descField)
            add(Box.createVerticalStrut(6))
            add(JLabel("System prompt addition:"))
            add(JBScrollPane(promptField))
        }

        val result = JOptionPane.showConfirmDialog(
            this, panel, "Create Agent Persona",
            JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE,
        )
        if (result != JOptionPane.OK_OPTION) return

        val name = nameField.text.trim()
        val desc = descField.text.trim()
        val prompt = promptField.text.trim()
        if (name.isEmpty()) return

        Thread {
            try {
                val agent = client.createAgent(projectId, name, desc, prompt)
                SwingUtilities.invokeLater {
                    agentsCombo.addItem(agent)
                    agentsCombo.selectedItem = agent
                    currentAgentId = agent.id
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    appendToChat("⚠ Could not create agent: ${e.message ?: "Unknown error"}\n\n")
                }
            }
        }.start()
    }

    // ── Chat ──────────────────────────────────────────────────────────────

    private fun sendMessage() {
        var message = inputField.text.trim()
        if (message.isEmpty()) return
        inputField.text = ""

        // ── Slash command handling ────────────────────────────────────────
        val scanCommand = when {
            message.startsWith("/full-scan")    -> "full"
            message.startsWith("/package-scan") -> "package"
            message.startsWith("/auto-scan")    -> "auto"
            else -> null
        }
        if (scanCommand != null) {
            // Strip the slash command prefix to get the actual message
            val afterCommand = message.substringAfter(" ", "").trim()
            handleScanCommand(scanCommand, afterCommand)
            return
        }

        inputField.isEnabled = false
        sendButton.isEnabled = false
        appendToChat("You: $message\n")

        // Reserve a position for the streamed response
        val responseStart = chatArea.document.length

        // Show initial thinking status with spinner
        var statusLineStart = responseStart
        var currentToolLabel: String? = null
        chatArea.append("${spinnerFrames[0]} CodeLM is thinking...\n")
        val statusTimer = Timer(80) {
            spinnerIndex = (spinnerIndex + 1) % spinnerFrames.size
            SwingUtilities.invokeLater {
                val label = currentToolLabel ?: "CodeLM is thinking..."
                val doc = chatArea.document
                val len = doc.length - statusLineStart
                if (len > 0) doc.remove(statusLineStart, len)
                doc.insertString(statusLineStart, "${spinnerFrames[spinnerIndex]} $label\n", null)
                chatArea.caretPosition = doc.length
            }
        }
        statusTimer.start()

        var firstChunk = true
        var receivedAnyText = false

        Thread {
            try {
                client.chatStream(
                    projectId = projectId,
                    message = message,
                    conversationId = session.conversationId,
                    sessionId = currentSessionId,
                    agentId = currentAgentId,
                    onChunk = { chunk ->
                        receivedAnyText = true
                        SwingUtilities.invokeLater {
                            if (firstChunk) {
                                firstChunk = false
                                statusTimer.stop()
                                val doc = chatArea.document
                                val len = doc.length - statusLineStart
                                if (len > 0) doc.remove(statusLineStart, len)
                                doc.insertString(statusLineStart, "CodeLM: $chunk", null)
                            } else {
                                chatArea.append(chunk)
                            }
                            chatArea.caretPosition = chatArea.document.length
                        }
                    },
                    onTool = { toolName ->
                        SwingUtilities.invokeLater {
                            if (!firstChunk) {
                                chatArea.append("\n")
                                statusLineStart = chatArea.document.length
                                chatArea.append("${spinnerFrames[0]} Querying $toolName...\n")
                                firstChunk = true
                                statusTimer.start()
                            }
                            currentToolLabel = "Querying $toolName..."
                        }
                    },
                    onAgent = { agentName ->
                        SwingUtilities.invokeLater {
                            currentToolLabel = "[$agentName] thinking..."
                        }
                    },
                    onFileEdit = { proposal ->
                        handleFileEditProposal(proposal)
                    },
                )

                SwingUtilities.invokeLater {
                    statusTimer.stop()
                    if (!receivedAnyText) {
                        val doc = chatArea.document
                        val len = doc.length - statusLineStart
                        if (len > 0) doc.remove(statusLineStart, len)
                        doc.insertString(statusLineStart, "CodeLM: (No text in response)\n\n", null)
                    } else {
                        chatArea.append("\n\n")
                    }
                    chatArea.caretPosition = chatArea.document.length
                    inputField.isEnabled = true
                    sendButton.isEnabled = true
                    inputField.requestFocus()
                    refreshSessionsCombo()
                }
            } catch (e: BackendException) {
                SwingUtilities.invokeLater {
                    statusTimer.stop()
                    val doc = chatArea.document
                    val len = doc.length - responseStart
                    if (len > 0) doc.remove(responseStart, len)
                    doc.insertString(responseStart, "⚠ Error (${e.statusCode}): ${e.message}\n\n", null)
                    inputField.isEnabled = true
                    sendButton.isEnabled = true
                    inputField.requestFocus()
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    statusTimer.stop()
                    val doc = chatArea.document
                    val len = doc.length - responseStart
                    if (len > 0) doc.remove(responseStart, len)
                    doc.insertString(responseStart, "⚠ Error: ${e.message ?: "Unknown error"}\n\n", null)
                    inputField.isEnabled = true
                    sendButton.isEnabled = true
                    inputField.requestFocus()
                }
            }
        }.start()
    }

    // ── Slash command scan trigger ────────────────────────────────────────

    private fun handleScanCommand(mode: String, messageAfter: String) {
        val (scanMode, folderPath, entryPoint, label) = when (mode) {
            "full" -> ScanCommandArgs("full", null, null, "Full project scan")
            "package" -> {
                // Use currently open file's directory as package path
                val openFile = FileEditorManager.getInstance(project).selectedFiles.firstOrNull()
                val dir = openFile?.parent?.path ?: session.rootPath
                ScanCommandArgs("folder", dir, null, "Package scan: $dir")
            }
            else -> {
                // auto-scan: use message keywords as entry point hint
                val hint = messageAfter.ifEmpty { null }
                ScanCommandArgs("smart", null, hint, "Auto-scan${if (hint != null) ": $hint" else ""}")
            }
        }
        appendToChat("You: /${mode}${if (messageAfter.isNotEmpty()) " $messageAfter" else ""}\n")
        startScanAnimation(label)
        Thread {
            try {
                val result = client.scanProject(
                    projectId = projectId,
                    rootPath = session.rootPath,
                    scanMode = scanMode,
                    folderPath = folderPath,
                    entryPoint = entryPoint,
                )
                stopScanAnimation("✓ Scan complete — ${result.files_found} files, ${result.classes_found} classes indexed")
                // If there was a message after the command, send it now
                if (messageAfter.isNotEmpty()) {
                    SwingUtilities.invokeLater {
                        inputField.text = messageAfter
                        sendMessage()
                    }
                }
            } catch (e: Exception) {
                stopScanAnimation("⚠ Scan failed: ${e.message ?: "Unknown error"}")
            }
        }.start()
    }

    private data class ScanCommandArgs(
        val mode: String,
        val folderPath: String?,
        val entryPoint: String?,
        val label: String,
    )

    // ── File edit proposal dialog ─────────────────────────────────────────

    /**
     * Shows an accept/reject dialog for a proposed file edit.
     * Must be called from a background thread (will invokeLater for UI).
     */
    private fun handleFileEditProposal(proposal: FileEditProposal) {
        if (acceptAllEdits) {
            applyFileEdit(proposal)
            SwingUtilities.invokeLater {
                appendToChat("\n✓ Applied: ${proposal.filePath} — ${proposal.description}\n")
            }
            return
        }

        SwingUtilities.invokeAndWait {
            val preview = buildString {
                appendLine("File: ${proposal.filePath}")
                appendLine()
                appendLine("Change: ${proposal.description}")
                if (proposal.originalSnippet.isNotEmpty()) {
                    appendLine()
                    appendLine("--- BEFORE ---")
                    appendLine(proposal.originalSnippet.take(400))
                }
                appendLine()
                appendLine("+++ AFTER +++")
                appendLine(proposal.newSnippet.take(400))
            }

            val options = arrayOf("Accept", "Accept All in this Chat", "Reject")
            val choice = JOptionPane.showOptionDialog(
                this,
                preview,
                "CodeLM — Proposed Edit",
                JOptionPane.DEFAULT_OPTION,
                JOptionPane.PLAIN_MESSAGE,
                null,
                options,
                options[0],
            )

            when (choice) {
                0 -> {
                    applyFileEdit(proposal)
                    appendToChat("\n✓ Applied: ${proposal.filePath} — ${proposal.description}\n")
                }
                1 -> {
                    acceptAllEdits = true
                    applyFileEdit(proposal)
                    appendToChat("\n✓ Applied (accept all): ${proposal.filePath} — ${proposal.description}\n")
                }
                else -> {
                    appendToChat("\n✗ Rejected: ${proposal.filePath}\n")
                }
            }
        }
    }

    private fun applyFileEdit(proposal: FileEditProposal) {
        try {
            val file = File(proposal.filePath)
            if (!file.exists() && proposal.originalSnippet.isEmpty()) {
                // New file
                file.parentFile?.mkdirs()
                file.writeText(proposal.newSnippet)
                return
            }
            val current = file.readText()
            val updated = if (proposal.originalSnippet.isNotEmpty()) {
                current.replace(proposal.originalSnippet, proposal.newSnippet)
            } else {
                current + "\n" + proposal.newSnippet
            }
            file.writeText(updated)
            // Refresh in IDE virtual file system
            com.intellij.openapi.vfs.LocalFileSystem.getInstance()
                .refreshAndFindFileByPath(proposal.filePath)
                ?.refresh(false, false)
        } catch (e: Exception) {
            SwingUtilities.invokeLater {
                appendToChat("⚠ Could not write file: ${e.message}\n")
            }
        }
    }

    private fun appendToChat(text: String) {
        chatArea.append(text)
        chatArea.caretPosition = chatArea.document.length
    }
}
