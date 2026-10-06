// Interactive command builder for the Garuda docs.
//
// Markup contract (see docs/guides/command-builder.md):
//   [data-garuda-builder][data-command]   container; data-command is the base command
//   .gb-options[data-group][data-multi]   a choice group; data-multi allows several picks
//   button[data-args]                     flags this choice adds (validated by
//                                         `scripts/check_docs.py --commands`)
//   button[data-task]                     default task text for a goal
//   button[data-explain] / [data-note]    explanation line / caution shown below
//   button[data-native-only]              unavailable when an external harness is chosen
//   button[data-external]                 marks an external (ACP) runtime choice
//   button[data-id] / [data-conflicts]    ids of choices this one can't combine with
//                                         (space-separated); they are disabled while it is on
//   input.gb-task, .gb-output code, ul.gb-explain, .gb-note
(function () {
  "use strict";

  function shellQuote(text) {
    return '"' + text.replace(/(["\\$`])/g, "\\$1") + '"';
  }

  // Show flags such as --mode as inline code so "--" stays legible.
  function fillWithFlags(element, text) {
    element.textContent = "";
    text.split(/(--[a-z][\w-]*)/).forEach(function (part, index) {
      if (!part) return;
      if (index % 2 === 1) {
        var code = document.createElement("code");
        code.textContent = part;
        element.appendChild(code);
      } else {
        element.appendChild(document.createTextNode(part));
      }
    });
  }

  function setup(root) {
    if (root.dataset.ready === "true") return;
    root.dataset.ready = "true";

    var groups = {};
    root.querySelectorAll(".gb-options[data-group]").forEach(function (group) {
      groups[group.dataset.group] = group;
      group.setAttribute("role", "group");
      var label = group.parentElement && group.parentElement.querySelector(".gb-label");
      if (label) group.setAttribute("aria-label", label.textContent.replace(/^\d+\s*·\s*/, ""));
      group.querySelectorAll("button").forEach(function (button, index) {
        button.type = "button";
        if (!group.hasAttribute("data-multi") && index === 0) {
          button.setAttribute("aria-pressed", "true");
        } else if (!button.hasAttribute("aria-pressed")) {
          button.setAttribute("aria-pressed", "false");
        }
        button.addEventListener("click", function () {
          if (button.disabled) return;
          if (group.hasAttribute("data-multi")) {
            var pressed = button.getAttribute("aria-pressed") === "true";
            button.setAttribute("aria-pressed", pressed ? "false" : "true");
            if (!pressed && button.dataset.conflicts) {
              button.dataset.conflicts.split(/\s+/).forEach(function (id) {
                var other = id && root.querySelector('button[data-id="' + id + '"]');
                if (other) other.setAttribute("aria-pressed", "false");
              });
            }
          } else {
            group.querySelectorAll("button").forEach(function (other) {
              other.setAttribute("aria-pressed", other === button ? "true" : "false");
            });
            if (group.dataset.group === "goal" && !taskEdited && button.dataset.task) {
              task.value = button.dataset.task;
            }
          }
          render();
        });
      });
    });

    var task = root.querySelector("input.gb-task");
    var output = root.querySelector(".gb-output code");
    var explain = root.querySelector("ul.gb-explain");
    var note = root.querySelector(".gb-note");
    var taskEdited = false;

    var firstGoal = groups.goal && groups.goal.querySelector("button");
    if (task && firstGoal && firstGoal.dataset.task) task.value = firstGoal.dataset.task;
    if (task) {
      task.addEventListener("input", function () {
        taskEdited = true;
        render();
      });
    }

    function selected(name) {
      var group = groups[name];
      if (!group) return [];
      return Array.prototype.filter.call(group.querySelectorAll("button"), function (button) {
        return button.getAttribute("aria-pressed") === "true" && !button.disabled;
      });
    }

    function render() {
      var engine = selected("engine")[0];
      var external = Boolean(engine && engine.hasAttribute("data-external"));

      // An external harness runs as its own process on this machine, so the
      // workspace and native-only extras do not apply to it.
      Object.keys(groups).forEach(function (name) {
        var group = groups[name];
        var buttons = group.querySelectorAll("button");
        buttons.forEach(function (button) {
          var disable = external && button.hasAttribute("data-native-only");
          button.disabled = disable;
          if (disable && !group.hasAttribute("data-multi") &&
              button.getAttribute("aria-pressed") === "true") {
            button.setAttribute("aria-pressed", "false");
            buttons[0].setAttribute("aria-pressed", "true");
          }
        });
      });

      // A pressed choice disables the choices it can't be combined with.
      var blocked = {};
      root.querySelectorAll("button[data-conflicts]").forEach(function (button) {
        if (button.disabled || button.getAttribute("aria-pressed") !== "true") return;
        button.dataset.conflicts.split(/\s+/).forEach(function (id) {
          if (id) blocked[id] = true;
        });
      });
      root.querySelectorAll("button[data-id]").forEach(function (button) {
        if (!blocked[button.dataset.id]) return;
        button.disabled = true;
        button.setAttribute("aria-pressed", "false");
      });

      var picks = [];
      ["goal", "where", "engine", "extras"].forEach(function (name) {
        selected(name).forEach(function (button) {
          // Run modes and profiles belong to the native loop; the external
          // harness decides how to approach the task itself.
          if (external && name === "goal") return;
          picks.push({
            args: button.dataset.args || "",
            explain: button.dataset.explain || "",
            note: button.dataset.note || "",
          });
        });
      });

      var parts = [root.dataset.command];
      picks.forEach(function (pick) {
        if (pick.args) parts.push(pick.args);
      });
      var text = task && task.value.trim() ? task.value.trim() : "Describe your task here";
      parts.push("-t " + shellQuote(text));
      if (output) output.textContent = parts.join(" ");

      if (explain) {
        explain.innerHTML = "";
        picks.forEach(function (pick) {
          if (!pick.explain) return;
          var item = document.createElement("li");
          fillWithFlags(item, pick.explain);
          explain.appendChild(item);
        });
      }
      if (note) {
        var notes = picks.map(function (pick) { return pick.note; }).filter(Boolean);
        fillWithFlags(note, notes.join(" "));
      }
    }

    render();
  }

  function init() {
    document.querySelectorAll("[data-garuda-builder]").forEach(setup);
  }

  if (typeof window.document$ !== "undefined" && window.document$.subscribe) {
    window.document$.subscribe(init);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
