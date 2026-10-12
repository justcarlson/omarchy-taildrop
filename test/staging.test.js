const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

// Exercise the actual QML functions and completion callback without a desktop.
const qml = fs.readFileSync(path.join(__dirname, "../Taildrop.qml"), "utf8")
const functions = [...qml.matchAll(/^  function \w+\([^\n]*\) \{\n[\s\S]*?^  \}/gm)]
  .map(match => match[0]).join("\n")
const processBlock = qml.match(/  Process \{\n    id: stageProcess\n[\s\S]*?\n  \}/)[0]
const callback = processBlock.match(/onExited: (function\(exitCode\) \{[\s\S]*?\n    \})/)[1]

function sheet() {
  const root = {
    opened: false, phase: "choose", stageRequestSerial: 0,
    pendingStageCommand: null, payloadStamp: 0, payloadFiles: [],
    sendSh: "/reviewed/send.sh", refresh() {}
  }
  const stageProcess = { running: false, requestSerial: -1 }
  const stageStdout = { text: "" }
  const context = vm.createContext({
    root, stageProcess, stageStdout, console,
    Qt: { callLater() {} }
  })
  vm.runInContext(functions, context)
  for (const name of ["open", "close", "dismiss", "setFilesPayload", "runStage",
    "invalidateStage", "startStage", "finishStage", "applyStaged"])
    root[name] = context[name]
  const exited = vm.runInContext("(" + callback + ")", context)
  function complete(preview) {
    stageProcess.running = false
    stageStdout.text = JSON.stringify({ kind: "text", source: "clipboard",
      path: "/private/clipboard.txt", preview })
    exited(0)
  }
  return { root, stageProcess, complete }
}

test("late clipboard completion cannot replace a Nautilus selection after reopen", () => {
  const s = sheet()
  s.root.open("{}")
  s.root.close()
  s.root.open(JSON.stringify({ files: ["/selected/report.txt"], source: "nautilus" }))
  s.complete("old clipboard")
  assert.equal(s.root.payloadKind, "files")
  assert.deepEqual(Array.from(s.root.payloadFiles), ["/selected/report.txt"])
  assert.equal(s.root.payloadSource, "nautilus")
  assert.equal(s.root.payloadPath, "")
})

test("closing the sheet discards a pending clipboard result", () => {
  const s = sheet()
  s.root.open("{}")
  s.root.dismiss()
  s.complete("old clipboard")
  assert.equal(s.root.opened, false)
  assert.equal(s.root.payloadStamp, 0)
})

test("a newer staging request runs after the old one and only its result is applied", () => {
  const s = sheet()
  s.root.open("{}")
  s.root.close()
  s.root.open("{}")
  s.complete("old clipboard")
  assert.equal(s.root.payloadKind, "staging")
  assert.equal(s.root.payloadStamp, 0)
  assert.equal(s.stageProcess.running, true)
  s.complete("new clipboard")
  assert.equal(s.root.payloadKind, "text")
  assert.equal(s.root.payloadPreview, "new clipboard")
  assert.equal(s.root.payloadStamp, 1)
})

test("selecting files clears a queued staging request", () => {
  const s = sheet()
  s.root.open("{}")
  s.root.runStage([s.root.sendSh, "stage-image"])
  s.root.setFilesPayload(["/selected/image.png"], "chooser")
  s.complete("old clipboard")
  assert.equal(s.stageProcess.running, false)
  assert.equal(s.root.pendingStageCommand, null)
  assert.equal(s.root.payloadKind, "files")
  assert.deepEqual(Array.from(s.root.payloadFiles), ["/selected/image.png"])
})
