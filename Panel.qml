import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Claude Code sessions: one bar icon that says whether anything is working,
// watching or waiting on you, and a panel listing every session. Picking a
// session focuses its terminal window.
Panel {
  id: root
  moduleName: "mike.claude-sessions"
  ipcTarget: "mike.claude-sessions"
  manageIpc: false

  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")).replace(/\/$/, "")
  readonly property string notifyMode: {
    var value = String(setting("notify", "Unfocused")).toLowerCase()
    return value === "always" || value === "off" ? value : "unfocused"
  }

  // Items inside the panel's lazily built content, null until it first opens.
  readonly property Item panelFlick: panel.loadedContent ? panel.loadedContent.panelFlick : null
  readonly property Item rowColumn: panel.loadedContent ? panel.loadedContent.rowColumn : null
  readonly property Item heroItem: panel.loadedContent ? panel.loadedContent.hero : null
  readonly property Item topSeparatorItem: panel.loadedContent ? panel.loadedContent.topSeparator : null
  readonly property Item bottomSeparatorItem: panel.loadedContent ? panel.loadedContent.bottomSeparator : null
  readonly property Item newRowItem: panel.loadedContent ? panel.loadedContent.newSessionRow : null

  property var sessions: []
  property var counts: ({ needs: 0, done: 0, working: 0, watching: 0, idle: 0 })
  property bool received: false
  property real markAngle: 0
  property double nowMs: Date.now()
  property bool cursorActive: false
  property int cursorIndex: 0

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property int needsCount: counts.needs || 0
  readonly property int doneCount: counts.done || 0
  readonly property bool anyWorking: (counts.working || 0) > 0
  readonly property bool anySessions: sessions.length > 0
  // Claude's ✳ mark. The bar font has no ✳ of its own, and Qt's fallback
  // picks the colour emoji font for it (a green square), even with U+FE0E,
  // so the mark names a text font that has it.
  readonly property string mark: "✳"
  readonly property string markFont: {
    var families = Qt.fontFamilies()
    var wanted = ["Noto Sans Symbols 2", "Adwaita Mono", "DejaVu Sans"]
    for (var i = 0; i < wanted.length; i++)
      if (families.indexOf(wanted[i]) >= 0) return wanted[i]
    return fontFamily
  }
  // The mark turns while something works; it holds still when a session
  // needs you, so the urgent colour and count read clearly.
  readonly property bool spinning: anyWorking && needsCount === 0 && !Style.reduceMotion

  readonly property color barIconColor: needsCount > 0
    ? urgent
    : (anySessions ? barForeground : Qt.darker(barForeground, 1.55))
  readonly property string barLabel: needsCount > 0
    ? Commons.I18n.formatNumber(needsCount)
    : (doneCount > 0 ? Commons.I18n.formatNumber(doneCount) : "")

  readonly property var stateOrder: ["needs", "done", "working", "watching", "idle"]
  readonly property string summary: {
    var parts = []
    for (var i = 0; i < stateOrder.length; i++) {
      var n = counts[stateOrder[i]] || 0
      if (n > 0) parts.push(strings.tn("summary." + stateOrder[i], n).arg(Commons.I18n.formatNumber(n)))
    }
    return parts.length > 0 ? parts.join(" · ") : strings.t("summary.none")
  }

  // Every session, in sections in the order they matter.
  readonly property var sections: {
    var result = []
    for (var s = 0; s < stateOrder.length; s++) {
      var list = []
      for (var i = 0; i < sessions.length; i++)
        if (sessions[i].state === stateOrder[s]) list.push(sessions[i])
      if (list.length > 0) result.push({ state: stateOrder[s], sessions: list })
    }
    return result
  }

  // The panel never scrolls: the list fits the room the panel has. Rows have
  // two lines while there's room; when there isn't, all but the ones that need
  // you drop to one line; and if even that won't fit, the least pressing
  // sessions fold into one "N more" row, which opens the Conversations picker.
  readonly property real sectionGap: Style.space(12)
  readonly property real rowGap: Style.space(4)
  readonly property real fullRowHeight: Math.ceil(bodyMetrics.height + Style.space(1) + captionMetrics.height) + Style.spacing.rowPaddingX
  readonly property real compactRowHeight: Math.ceil(bodyMetrics.height) + Style.spacing.rowPaddingX
  readonly property real listRoom: {
    var cap = panel.fittedContentHeight(100000, Style.space(560)) - panel.verticalContentInset
    var fixed = (heroItem ? heroItem.height : 0) + (topSeparatorItem ? topSeparatorItem.height : 0)
      + (bottomSeparatorItem ? bottomSeparatorItem.height : 0) + (newRowItem ? newRowItem.height : 0)
      + 4 * sectionGap
    return cap - fixed - Style.space(4)
  }
  readonly property var plan: {
    var all = sections
    var room = listRoom
    var header = headerProbe.implicitHeight
    function rowHeight(state, compact) { return compact && state !== "needs" ? compactRowHeight : fullRowHeight }
    function total(compact) {
      var h = 0
      for (var g = 0; g < all.length; g++) {
        h += (g > 0 ? sectionGap : 0) + header
        for (var r = 0; r < all[g].sessions.length; r++) h += rowGap + rowHeight(all[g].state, compact)
      }
      return h
    }
    var compact = total(false) > room
    var shown = []
    var hidden = 0
    if (!compact || total(true) <= room) {
      for (var a = 0; a < all.length; a++) shown.push(all[a].sessions.length)
    } else {
      // Most pressing first, keeping room for the "N more" row at the end.
      var more = sectionGap + compactRowHeight
      var used = 0
      var full = false
      for (var g = 0; g < all.length; g++) {
        var count = 0
        while (!full && count < all[g].sessions.length) {
          var add = rowGap + rowHeight(all[g].state, true)
          if (count === 0) add += (used > 0 ? sectionGap : 0) + header
          if (used + add + more > room) { full = true; break }
          used += add
          count++
        }
        shown.push(count)
        hidden += all[g].sessions.length - count
      }
    }
    var result = []
    var index = 0
    for (var k = 0; k < all.length; k++) {
      if (shown[k] === 0) continue
      var rows = []
      for (var r = 0; r < shown[k]; r++) rows.push({ session: all[k].sessions[r], index: index++ })
      result.push({ state: all[k].state, rows: rows })
    }
    return { groups: result, compact: compact, hidden: hidden }
  }
  readonly property var groups: plan.groups
  readonly property bool compactRows: plan.compact
  readonly property int hiddenCount: plan.hidden
  readonly property var orderedSessions: {
    var list = []
    for (var g = 0; g < groups.length; g++)
      for (var r = 0; r < groups[g].rows.length; r++) list.push(groups[g].rows[r].session)
    return list
  }
  // The keyboard order: every session row shown, the "N more" row if there is
  // one, then "New session".
  readonly property int moreIndex: hiddenCount > 0 ? orderedSessions.length : -1
  readonly property int newRowIndex: orderedSessions.length + (hiddenCount > 0 ? 1 : 0)
  readonly property int cursorCount: newRowIndex + 1

  FontMetrics { id: bodyMetrics; font.family: root.fontFamily; font.pixelSize: Style.font.body }
  FontMetrics { id: captionMetrics; font.family: root.fontFamily; font.pixelSize: Style.font.caption }
  PanelSectionHeader { id: headerProbe; visible: false; text: "Idle"; fontFamily: root.fontFamily }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  // ---------------------------------------------------------------- data

  function limited(value, limit) {
    var s = value === undefined || value === null ? "" : String(value)
    return s.length > limit ? s.substring(0, limit) : s
  }

  // `omarchy-shell mike.claude-sessions preview 14` shows made-up sessions for
  // a minute, to see the panel crowded; then the real ones come back.
  property double previewUntil: 0
  property string lastLine: ""
  function preview(count) {
    var n = Math.max(1, Math.min(64, Number(count) || 14))
    var states = ["needs", "needs", "done", "working", "working", "watching", "done", "idle"]
    var names = ["Super shift C shortcut for the conversations window", "Fix the flaky payments webhook retries",
      "RAVN landing copy pass", "Refactor the notch hover timing", "Buildr creator directory search",
      "Contextual teaser sky shader", "Birthday scrapbook PIN lock", "Dock flares on every edge",
      "Write the release notes", "Translate the README", "Bump Hyprland and test the Lua config"]
    var list = []
    for (var i = 0; i < n; i++)
      list.push({ id: "preview-" + i, label: names[i % names.length], project: "preview", state: states[Math.min(i, states.length - 1)],
        reason: i < 2 ? "permission" : "", detail: "", since: Date.now() - i * 420000, window: "", tmux: false })
    showLine(JSON.stringify({ sessions: list }))
    previewUntil = Date.now() + 60000
    previewEnd.restart()
  }
  Timer {
    id: previewEnd
    interval: 60000
    onTriggered: { root.previewUntil = 0; if (root.lastLine !== "") root.showLine(root.lastLine) }
  }

  // Every line from the collector is kept; a preview only changes what's shown.
  function applyLine(line) {
    lastLine = line
    if (Date.now() < previewUntil) return
    showLine(line)
  }

  function showLine(line) {
    var data
    try { data = JSON.parse(line) } catch (e) { return }
    if (!data || !Array.isArray(data.sessions)) return
    var list = []
    var tally = { needs: 0, done: 0, working: 0, watching: 0, idle: 0 }
    for (var i = 0; i < data.sessions.length && i < 64; i++) {
      var s = data.sessions[i]
      if (!s || typeof s !== "object") continue
      var state = tally.hasOwnProperty(s.state) ? s.state : "idle"
      var window = /^0x[0-9a-f]{1,16}$/.test(String(s.window || "")) ? String(s.window) : ""
      tally[state]++
      list.push({
        id: limited(s.id, 120),
        label: limited(s.label, 120),
        project: limited(s.project, 60),
        state: state,
        reason: limited(s.reason, 20),
        detail: limited(s.detail, 120),
        since: Number(s.since) > 0 ? Number(s.since) : 0,
        window: window,
        tmux: s.tmux === true
      })
    }
    sessions = list
    counts = tally
    received = true
    if (cursorIndex >= cursorCount) cursorIndex = Math.max(0, cursorCount - 1)
  }

  function ago(since) {
    if (!since) return ""
    var minutes = Math.floor(Math.max(0, nowMs - since) / 60000)
    if (minutes < 1) return strings.t("ago.now")
    if (minutes < 60) return strings.t("ago.minutes").arg(Commons.I18n.formatNumber(minutes))
    var hours = Math.floor(minutes / 60)
    if (hours < 24) return strings.t("ago.hours").arg(Commons.I18n.formatNumber(hours))
    return strings.t("ago.days").arg(Commons.I18n.formatNumber(Math.floor(hours / 24)))
  }

  function stateLine(session) {
    if (session.state === "needs") {
      if (session.reason !== "other" && session.reason !== "") return strings.t("reason." + session.reason, strings.t("state.needs"))
      return session.detail !== "" ? strings.t("reason.other").arg(session.detail) : strings.t("state.needs")
    }
    if (session.state === "idle") return session.detail !== "" ? strings.t("detail.idleRaw").arg(session.detail) : ""
    if (session.state === "working") return ""
    return strings.t("detail." + session.state)
  }

  function metaLine(session) {
    var parts = []
    var what = stateLine(session)
    if (what !== "") parts.push(what)
    if (session.project !== "") parts.push(session.project)
    if (session.window === "") parts.push(session.tmux ? strings.t("inTmux") : strings.t("noWindow"))
    var when = ago(session.since)
    if (when !== "") parts.push(when)
    return parts.join(" · ")
  }

  function stateGlyph(state) {
    if (state === "working") return mark
    if (state === "watching") return "◉"
    if (state === "idle") return "○"
    return "●"
  }

  function stateColor(state) {
    if (state === "needs") return urgent
    if (state === "idle") return dim
    return foreground
  }

  // ------------------------------------------------------------- actions

  function focusSession(session) {
    if (!session || session.window === "") return false
    // The address is checked against ^0x[0-9a-f]+$ when it arrives.
    Hyprland.dispatch('hl.dsp.focus({ window = "address:' + session.window + '" })')
    close()
    return true
  }

  // The session that has waited on you longest, then the oldest finished one.
  function jump() {
    for (var i = 0; i < sessions.length; i++)
      if (sessions[i].state === "needs" && focusSession(sessions[i])) return "ok"
    for (var j = 0; j < sessions.length; j++)
      if (sessions[j].state === "done" && focusSession(sessions[j])) return "ok"
    if (!opened) open()
    return "none"
  }

  function newSession() {
    Quickshell.execDetached(["omarchy-agent"])
    close()
  }

  function moveCursor(dy) {
    if (dy === 0) return
    cursorIndex = Math.max(0, Math.min(cursorCount - 1, cursorIndex + dy))
    scrollCursorIntoView()
  }

  function activateCursor() {
    if (cursorIndex < orderedSessions.length) focusSession(orderedSessions[cursorIndex])
    else if (cursorIndex === moreIndex) showAll()
    else newSession()
  }

  // Every conversation, in the Super+Shift+C picker (from the notch plugin).
  function showAll() {
    Quickshell.execDetached(["sh", "-c", "command -v claude-conversations >/dev/null 2>&1 && exec omarchy-launch-or-focus-tui --app-id=org.omarchy.claude-conversations claude-conversations"])
    close()
  }

  function setCursor(index) {
    cursorActive = true
    cursorIndex = index
  }

  function scrollCursorIntoView() {
    if (!panelFlick || !rowColumn) return
    Qt.callLater(function() {
      var item = rowColumn.rowAt(root.cursorIndex)
      if (!item || !panelFlick) return
      var margin = Style.space(6)
      var top = item.mapToItem(panelFlick.contentItem, 0, 0).y
      var bottom = top + item.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  onOpenedChanged: if (opened) {
    nowMs = Date.now()
    cursorActive = false
    cursorIndex = 0
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function() { if (panel.loadedContent) panel.loadedContent.forceActiveFocus() })
  }

  // -------------------------------------------------------------- helper

  onNotifyModeChanged: if (collector.running) collector.running = false

  Process {
    id: collector
    command: ["python3", "-I", root.pluginDir + "/sessions.py", "--notify", root.notifyMode]
    stdout: SplitParser { onRead: function(line) { root.applyLine(line) } }
    onExited: restartTimer.start()
  }

  Timer {
    id: restartTimer
    interval: 2000
    onTriggered: if (root.pluginDir !== "") collector.running = true
  }

  Component.onCompleted: if (pluginDir !== "") collector.running = true

  // One slow, steady turn every four seconds while working (linear, as a
  // spinner should be). When the work stops, the mark eases on to the next
  // upright position (✳ looks the same every 45°) instead of freezing askew.
  onSpinningChanged: {
    if (spinning) {
      settle.stop()
      spin.from = markAngle % 360
      spin.to = spin.from + 360
      spin.restart()
    } else {
      spin.stop()
      var angle = markAngle % 360
      if (angle === 0) return
      settle.from = angle
      settle.to = Math.ceil(angle / 45) * 45
      settle.restart()
    }
  }
  Component.onDestruction: { spin.stop(); settle.stop() }

  NumberAnimation {
    id: spin
    target: root
    property: "markAngle"
    duration: 4000
    loops: Animation.Infinite
  }

  NumberAnimation {
    id: settle
    target: root
    property: "markAngle"
    duration: Style.duration(240)
    easing.type: Easing.OutCubic
    onFinished: root.markAngle = 0
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    onTriggered: root.nowMs = Date.now()
  }

  ShellIpc {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function jump(): string { return root.jump() }
    function status(): string { return root.summary }
    function preview(count: int): void { root.preview(count) }
    // How the list is fitted right now (for testing).
    function layout(): string {
      return JSON.stringify({ room: root.listRoom, fullRow: root.fullRowHeight, compactRow: root.compactRowHeight,
        header: headerProbe.implicitHeight, compact: root.compactRows, shown: root.orderedSessions.length,
        hidden: root.hiddenCount, loaded: !!root.heroItem })
    }
  }

  // ----------------------------------------------------------------- bar

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // The bar fits this text to its icon size by the glyph's ink. It turns
    // through a Rotation transform about that ink's centre: the bar re-measures
    // an icon when its `rotation` property changes, which would make it pulse.
    iconComponent: Component {
      Text {
        id: barMark
        textFormat: Text.PlainText
        text: root.mark
        color: root.barIconColor
        font.family: root.markFont
        font.pixelSize: 32

        TextMetrics { id: barInk; font: barMark.font; text: barMark.text }

        transform: Rotation {
          origin.x: barInk.tightBoundingRect.x + barInk.tightBoundingRect.width / 2
          origin.y: barMark.baselineOffset + barInk.tightBoundingRect.y + barInk.tightBoundingRect.height / 2
          angle: root.markAngle
        }
      }
    }
    foreground: root.barIconColor
    labelText: root.barLabel
    tooltipText: root.opened ? "" : root.summary
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.jump()
      else if (buttonCode === Qt.MiddleButton) root.newSession()
      else root.toggle()
    }
  }

  // --------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(560))

    content: PanelKeyCatcher {
      id: keyCatcher
      property alias panelFlick: panelFlick
      property alias rowColumn: rowColumn
      property alias hero: hero
      property alias topSeparator: topSeparator
      property alias bottomSeparator: bottomSeparator
      property alias newSessionRow: newRow
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "n" || t === "N") root.newSession()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        // The list is fitted to the panel (see `plan`), so this never needs
        // to scroll, and shows no scroll bar.
        interactive: contentHeight > height + 1

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: strings.t("title")
            meta: root.received ? root.summary : strings.t("starting")
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.mark
                color: root.needsCount > 0 ? root.urgent : (root.anySessions ? root.foreground : root.dim)
                font.family: root.markFont
                font.pixelSize: Style.font.display
              }
            }
          }

          PanelSeparator {
            id: topSeparator
            visible: root.anySessions
            foreground: root.foreground
          }

          Column {
            id: rowColumn
            visible: root.anySessions
            width: parent.width
            spacing: Style.space(12)

            function rowAt(index) {
              for (var g = 0; g < groupRepeater.count; g++) {
                var group = groupRepeater.itemAt(g)
                var row = group ? group.rowAt(index) : null
                if (row) return row
              }
              if (index === root.moreIndex) return moreRow
              return index === root.newRowIndex ? newRow : null
            }

            Repeater {
              id: groupRepeater
              model: root.groups

              Column {
                id: groupColumn
                required property var modelData
                width: rowColumn.width
                spacing: Style.space(4)

                function rowAt(index) {
                  for (var r = 0; r < rowRepeater.count; r++) {
                    var row = rowRepeater.itemAt(r)
                    if (row && row.rowIndex === index) return row
                  }
                  return null
                }

                PanelSectionHeader {
                  text: strings.t("state." + groupColumn.modelData.state)
                  foreground: groupColumn.modelData.state === "needs" ? root.urgent : root.foreground
                  fontFamily: root.fontFamily
                }

                Repeater {
                  id: rowRepeater
                  model: groupColumn.modelData.rows

                  SessionRow {
                    required property var modelData
                    width: groupColumn.width
                    session: modelData.session
                    rowIndex: modelData.index
                  }
                }
              }
            }

            MoreRow {
              id: moreRow
              visible: root.hiddenCount > 0
              width: parent.width
            }
          }

          Text {
            visible: root.received && !root.anySessions
            width: parent.width
            text: strings.t("empty")
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
          }

          PanelSeparator { id: bottomSeparator; foreground: root.foreground }

          NewSessionRow {
            id: newRow
            width: parent.width
          }
        }
      }
    }
  }

  component SessionRow: CursorSurface {
    id: sessionRow
    property var session: ({})
    property int rowIndex: 0
    readonly property bool reachable: session && session.window !== ""

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: sessionRow.reachable ? Qt.PointingHandCursor : Qt.ArrowCursor
      onEntered: root.setCursor(sessionRow.rowIndex)
      onClicked: root.focusSession(sessionRow.session)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      RowGlyph {
        readonly property bool working: sessionRow.session.state === "working"
        text: root.stateGlyph(sessionRow.session.state)
        color: root.stateColor(sessionRow.session.state)
        fontFamily: working ? root.markFont : root.fontFamily
        // OpticalGlyph centres the ink in its box, so this turns it in place.
        rotation: working ? root.markAngle : 0
      }

      ColumnLayout {
        id: rowContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: sessionRow.session.label
          color: sessionRow.session.state === "idle" ? root.dim : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          visible: !root.compactRows || sessionRow.session.state === "needs"
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: root.metaLine(sessionRow.session)
          color: sessionRow.session.state === "needs" ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component MoreRow: CursorSurface {
    id: moreSessionRow
    readonly property int rowIndex: root.moreIndex

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: moreText.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setCursor(moreSessionRow.rowIndex)
      onClicked: root.showAll()
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      RowGlyph {
        text: "…"
        color: root.dim
      }

      Text {
        id: moreText
        textFormat: Text.PlainText
        Layout.fillWidth: true
        text: strings.t("more").arg(Commons.I18n.formatNumber(root.hiddenCount))
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
      }
    }
  }

  component NewSessionRow: CursorSurface {
    id: newSessionRow
    readonly property int rowIndex: root.newRowIndex

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: newContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setCursor(newSessionRow.rowIndex)
      onClicked: root.newSession()
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      RowGlyph {
        text: "+"
        color: root.foreground
        paintedSize: Style.space(10)
      }

      ColumnLayout {
        id: newContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: strings.t("newSession")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: strings.t("newSessionHint")
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component RowGlyph: OpticalGlyph {
    fontFamily: root.fontFamily
    fontSize: Style.font.icon
    paintedSize: Style.space(12)
    Layout.alignment: Qt.AlignVCenter
    Layout.preferredWidth: Style.space(16)
    Layout.preferredHeight: Style.space(16)
  }
}
