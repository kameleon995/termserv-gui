/* termserv Cockpit plugin.
 *
 * Talks only to termservctl (through cockpit.spawn), so it validates and behaves
 * exactly like the built-in web UI and the TUI. Changes need Cockpit's
 * "administrative access" (sudo); viewing works without it where permissions allow.
 */
"use strict";

const CTL = "/usr/local/bin/termservctl";
const $ = (id) => document.getElementById(id);

let state = null;
let logPort = null;
let logTimer = null;
let me = "user";  // the Cockpit login name, for the ssh link
cockpit.user().then((u) => { if (u && u.name) { me = u.name; if (state) render(); } });

/* --- theme: follow Cockpit's light/dark setting ----------------------------- */
function applyTheme() {
    let style = "auto";
    try { style = localStorage.getItem("shell:style") || "auto"; } catch (e) { /* ignore */ }
    const dark = style === "dark" || (style === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
}
applyTheme();
window.addEventListener("storage", applyTheme);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

/* --- running termservctl ---------------------------------------------------- */
function ctl(args, opts) {
    opts = opts || {};
    const proc = cockpit.spawn([CTL].concat(args), {
        superuser: opts.write ? "require" : "try",
        err: "message",
    });
    if (opts.input !== undefined)
        proc.input(opts.input);
    return proc;
}

function errorText(err) {
    if (err && err.problem === "access-denied")
        return "Turn on administrative access (top right of Cockpit) to change serial ports.";
    if (err && err.problem === "not-found")
        return "termservctl was not found. Is termserv installed (sudo ./install.sh)?";
    const msg = (err && (err.message || err.problem)) || String(err);
    return msg.replace(/^error: /, "");
}

function showAlert(text, ok) {
    const el = $("alert");
    el.textContent = text;
    el.classList.toggle("ok", !!ok);
    el.hidden = !text;
}

/* --- port table ------------------------------------------------------------- */
function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
}

function cell(row, label, content) {
    const td = el("td");
    td.dataset.label = label;
    if (content instanceof Node) td.appendChild(content);
    else td.textContent = content;
    row.appendChild(td);
    return td;
}

function button(text, onClick, cls) {
    const b = el("button", text, cls || "link");
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
}

// ssh:// opens the user's own SSH client; nothing runs in the browser.
function connectLink(p, address) {
    const cmd = `ssh -p ${p.ssh_port} ${me}@${address}`;
    const a = el("a", "Connect", "link");
    a.href = `ssh://${encodeURIComponent(me)}@${address}:${p.ssh_port}`;
    a.title = "Open in your SSH client: " + cmd;
    const copy = button("Copy ssh", () => {
        const done = (ok) => {
            if (!ok) { window.prompt("Copy this command:", cmd); return; }
            copy.textContent = "Copied";
            setTimeout(() => { copy.textContent = "Copy ssh"; }, 1500);
        };
        if (navigator.clipboard && window.isSecureContext)
            navigator.clipboard.writeText(cmd).then(() => done(true), () => done(false));
        else
            done(false);
    });
    copy.title = "Copy: " + cmd;
    return [a, copy];
}

function render() {
    const s = state;
    $("howto").textContent = `Connect with ssh <user>@${s.address} -p ${s.settings.base_port}+N`;
    const tbody = document.querySelector("#ports tbody");
    tbody.replaceChildren();
    for (const p of s.ports) {
        const tr = el("tr");
        cell(tr, "Port", String(p.number));
        cell(tr, "SSH", String(p.ssh_port));
        cell(tr, "Name", p.name || "–");
        const adapter = el("span", p.socket ? p.socket + (p.tty ? " → " + p.tty : "") : p.device_path);
        if (!p.device_present && p.state !== "disabled")
            adapter.appendChild(el("span", p.socket ? "empty socket" : "missing", "badge"));
        cell(tr, "Adapter", adapter);
        cell(tr, "Line", `${p.baud} 8${p.parity[0].toUpperCase()}1` + (p.flow && p.flow !== "none" ? ` ${p.flow}` : "") + (p.ondemand ? " · on demand" : "") + (p.log === false ? " · no log" : ""));
        cell(tr, "Access", (p.groups || []).join(", ") || s.settings.ssh_group);
        cell(tr, "State", el("span", p.state, "state-" + p.state));
        cell(tr, "Connected", (p.users || []).join(", ") || "–");
        const acts = el("span");
        acts.appendChild(button("Edit", () => openForm(p)));
        acts.appendChild(button("Log", () => openLog(p.number)));
        if (p.enabled && p.state !== "released")
            for (const n of connectLink(p, s.address)) acts.appendChild(n);
        if (p.enabled) {
            if (p.state === "released")
                acts.appendChild(button("Grab", () => run(["grab", String(p.number)], `Port ${p.number} is back under termserv.`)));
            else
                acts.appendChild(button("Release", () => confirmThen(
                    `Release port ${p.number}? Anyone connected is disconnected and the device is freed for local tools until you grab it back or reboot.`,
                    () => run(["release", String(p.number)], `Port ${p.number} released for local use.`))));
        }
        acts.appendChild(button("Delete", () => confirmThen(
            `Delete port ${p.number}${p.name ? " (" + p.name + ")" : ""}?`,
            () => run(["remove", String(p.number)], `Port ${p.number} deleted. Apply to make it live.`))));
        cell(tr, "", acts).className = "acts";
        tbody.appendChild(tr);
    }
    $("empty").hidden = s.ports.length > 0;
    $("pending").hidden = s.pending.length === 0;
    $("pending-text").textContent = s.pending.length ? `Saved changes are not live yet (${s.pending.join(", ")}).` : "";
    $("services").textContent = "Services: " + Object.entries(s.services).map(([k, v]) => `${k.replace(".service", "")} ${v}`).join(" · ");
}

function refresh() {
    return ctl(["state"])
        .then((out) => { state = JSON.parse(out); render(); })
        .catch((err) => showAlert(errorText(err)));
}

function run(args, okText) {
    showAlert("");
    return ctl(args, { write: true })
        .then((out) => showAlert(okText || out.trim(), true))
        .catch((err) => showAlert(errorText(err)))
        .then(refresh);
}

/* --- confirm dialog --------------------------------------------------------- */
function confirmThen(text, action) {
    const d = $("confirm-dialog");
    $("confirm-text").textContent = text;
    const done = (yes) => {
        d.close();
        $("confirm-yes").onclick = $("confirm-no").onclick = null;
        if (yes) action();
    };
    $("confirm-yes").onclick = () => done(true);
    $("confirm-no").onclick = () => done(false);
    d.showModal();
}

/* --- add / edit form -------------------------------------------------------- */
let editing = null;  // original port number, or null for a new port

function fillSelect(sel, items, value) {
    sel.replaceChildren();
    for (const [label, v, disabled] of items) {
        const o = el("option", label);
        o.value = v;
        o.disabled = !!disabled;
        sel.appendChild(o);
    }
    sel.value = String(value);
}

function openForm(port) {
    const isNew = !port;
    editing = isNew ? null : port.number;
    const p = port || { number: state.next_free, name: "", device: "", baud: 115200, parity: "none", flow: "none",
                        groups: [], usb_path: "", enabled: true, ondemand: false, log: true };
    $("form-title").textContent = isNew ? "Add port" : `Edit port ${p.number}`;
    $("form-error").textContent = "";
    $("f-number").value = p.number;
    $("f-name").value = p.name || "";
    $("f-device").value = p.device || "";
    $("f-device").placeholder = `/dev/port${p.number} (default)`;
    $("f-groups").value = (p.groups || []).join(", ");
    $("f-enabled").checked = !!p.enabled;
    $("f-ondemand").checked = !!p.ondemand;
    $("f-log").checked = p.log !== false;
    fillSelect($("f-baud"), state.bauds.map((b) => [String(b), b]), p.baud);
    fillSelect($("f-parity"), state.parities.map((x) => [x, x]), p.parity);
    fillSelect($("f-flow"), (state.flows || ["none"]).map((x) => [x, x]), p.flow || "none");
    fillSelect($("f-usb"), [["none (use device path)", ""]], "");
    $("form-dialog").showModal();
    // socket list: what is plugged in now, with sockets used by other ports disabled
    ctl(["state", "--sockets-for", String(isNew ? 0 : p.number)])
        .then((out) => {
            const choices = JSON.parse(out).sockets || [];
            fillSelect($("f-usb"), [["none (use device path)", ""]].concat(choices.map((c) => [
                c.label + (c.used_by ? `  (used by port ${c.used_by})` : ""), c.id_path, c.used_by])), p.usb_path || "");
        })
        .catch((err) => { $("form-error").textContent = errorText(err); });
}

function saveForm(ev) {
    ev.preventDefault();
    const data = {
        number: $("f-number").value,
        name: $("f-name").value,
        usb_path: $("f-usb").value,
        device: $("f-device").value,
        baud: $("f-baud").value,
        parity: $("f-parity").value,
        flow: $("f-flow").value,
        groups: $("f-groups").value,
        enabled: $("f-enabled").checked,
        ondemand: $("f-ondemand").checked,
        log: $("f-log").checked,
    };
    const args = ["save-port"].concat(editing === null ? [] : ["--original", String(editing)]);
    ctl(args, { write: true, input: JSON.stringify(data) })
        .then(() => {
            $("form-dialog").close();
            showAlert(`Port ${data.number} saved. Apply to make it live.`, true);
            refresh();
        })
        .catch((err) => { $("form-error").textContent = errorText(err); });
}

/* --- log view --------------------------------------------------------------- */
function loadLog() {
    ctl(["log", String(logPort)])
        .then((out) => {
            const pre = $("log");
            const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
            if (pre.textContent !== out) {
                const port = state && state.ports.find((x) => x.number === logPort);
                pre.textContent = out || (port && port.log === false ? "(logging is off for this port)" : "(nothing logged yet)");
                if ($("log-follow").checked && atBottom) pre.scrollTop = pre.scrollHeight;
            }
        })
        .catch((err) => { $("log").textContent = errorText(err); });
}

function openLog(n) {
    logPort = n;
    $("log-title").textContent = `Port ${n} log`;
    $("log").textContent = "";
    $("ports-view").hidden = true;
    $("log-view").hidden = false;
    loadLog();
    setTimeout(() => { $("log").scrollTop = $("log").scrollHeight; }, 300);
    logTimer = setInterval(loadLog, 3000);
}

function closeLog() {
    clearInterval(logTimer);
    logTimer = null;
    $("log-view").hidden = true;
    $("ports-view").hidden = false;
}

/* --- wiring ----------------------------------------------------------------- */
$("add").addEventListener("click", () => openForm(null));
$("users").addEventListener("click", () => cockpit.jump("/users"));
$("apply").addEventListener("click", () => {
    $("apply").disabled = true;
    run(["apply"]).then(() => { $("apply").disabled = false; });
});
$("restart").addEventListener("click", () => confirmThen(
    state ? state.restart_question : "Restart the termserv services?",
    () => {
        $("restart").disabled = true;
        run(["restart"]).then(() => { $("restart").disabled = false; });
    }));
$("port-form").addEventListener("submit", saveForm);
$("form-cancel").addEventListener("click", () => $("form-dialog").close());
$("log-back").addEventListener("click", closeLog);
$("log-clear").addEventListener("click", () => confirmThen(
    `Clear port ${logPort}'s log? Everything recorded so far, including rotated copies, is deleted for good.`,
    () => ctl(["clear-log", String(logPort), "--yes"], { write: true })
        .then(() => { $("log").textContent = ""; loadLog(); })
        .catch((err) => { $("log").textContent = errorText(err); })));

// re-check when admin access is switched on/off
const superuser = cockpit.dbus(null, { bus: "internal" }).proxy("cockpit.Superuser", "/superuser");
superuser.addEventListener("changed", refresh);

cockpit.transport.wait(() => {
    refresh();
    setInterval(() => { if (!logTimer && !document.hidden && !document.querySelector("dialog[open]")) refresh(); }, 5000);
});
