/* 浏览器内核验：逻辑与 verify/verify.py 对应，独立重算，不信任包内 winners。 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var out = $("verifyOut");

  function line(cls, html) {
    var div = document.createElement("div");
    div.className = "check-line check-" + cls;
    div.innerHTML = html;
    out.appendChild(div);
  }
  function heading(html) {
    var div = document.createElement("div");
    div.style.cssText = "margin:10px 0 4px;font-weight:700;";
    div.innerHTML = html;
    out.appendChild(div);
  }

  function hasDuplicates(arr) {
    return new Set(arr).size !== arr.length;
  }
  function duplicates(arr) {
    var seen = Object.create(null), ret = [];
    arr.forEach(function (x) {
      seen[x] = (seen[x] || 0) + 1;
    });
    Object.keys(seen).forEach(function (k) { if (seen[k] > 1) ret.push(k); });
    return ret;
  }

  function verifyPackage(pkg) {
    out.innerHTML = "";
    var failures = 0;
    function fail(msg) { failures++; line("fail", "✗ " + UI.esc(msg)); }
    function pass(msg) { line("pass", "✓ " + UI.esc(msg)); }
    function info(msg) { line("", UI.esc(msg)); }

    var rnd = pkg.round || {};
    heading("轮次：" + UI.esc(rnd.title || "?") + "（" + UI.esc(rnd.id || "?") + "）");
    info("状态：" + UI.esc(rnd.status) + "　名额：" + rnd.prizeCount +
         "　人数：" + (pkg.participantCount != null ? pkg.participantCount : "?"));
    heading("校验项");

    if (pkg.format !== "public-lottery-verification-v1") fail("format 字段不符：" + String(pkg.format));
    if (pkg.ruleId !== LotteryRules.RULE_ID) {
      fail("规则标识不符：" + String(pkg.ruleId) + "，本核验器只认 " + LotteryRules.RULE_ID);
    } else {
      pass("规则标识为 " + LotteryRules.RULE_ID);
    }

    var entries = pkg.participants;
    if (!Array.isArray(entries) || !entries.every(function (e) { return typeof e === "string"; })) {
      fail("participants 必须是字符串数组");
      return finish(failures);
    }

    var bad = entries.filter(function (e) {
      return !e.trim() || e !== e.trim() || e.indexOf("\n") >= 0 || e.indexOf("\r") >= 0;
    });
    if (bad.length) fail("名单含空编号/换行/未去首尾空白的编号：" + bad.slice(0, 5).join(", "));

    var dup = duplicates(entries);
    if (dup.length) fail("名单存在重复编号：" + dup.join(", "));

    var wantOrder = LotteryRules.canonicalSorted(entries);
    if (entries.length === wantOrder.length && entries.every(function (e, i) { return e === wantOrder[i]; })) {
      pass("名单为按 UTF-8 字节序排序的规范名单，且编号无重复");
    } else {
      var pos = 0;
      while (pos < entries.length && entries[pos] === wantOrder[pos]) pos++;
      fail("名单顺序不是规范的 UTF-8 字节序（首个偏差在第 " + (pos + 1) + " 项）");
    }

    var recalcDigest = LotteryRules.listDigest(entries);
    var extDigest = $("extDigest").value.trim().toLowerCase();
    var extCommit = $("extCommit").value.trim().toLowerCase();

    if (!rnd.listDigest) {
      fail("核验包缺少 listDigest（名单摘要）");
    } else if (recalcDigest !== rnd.listDigest.toLowerCase()) {
      fail("名单摘要不符：重算 " + recalcDigest + "；包内 " + rnd.listDigest);
    } else {
      pass("名单摘要核对一致：" + recalcDigest);
    }
    if (extDigest) {
      if (extDigest === recalcDigest && (!rnd.listDigest || extDigest === rnd.listDigest.toLowerCase())) {
        pass("与事先留存的名单摘要一致");
      } else {
        fail("事先留存的名单摘要不符：留存 " + extDigest + "；重算 " + recalcDigest);
      }
    }

    var commitment = (rnd.commitment || "").toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(commitment)) fail("承诺不是 64 位十六进制");
    if (extCommit) {
      if (extCommit === commitment) pass("与事先留存的承诺一致");
      else fail("事先留存的承诺与包内承诺不符：留存 " + extCommit + "；包内 " + commitment);
    }

    var seed = rnd.seed ? rnd.seed.trim().toLowerCase() : null;
    if (!seed) {
      heading("结论");
      if (rnd.status === "sealed") {
        info("该包处于封存（未揭晓）状态：承诺/名单摘要已核验，暂无种子与结果可校验。");
      } else {
        fail("包内无种子，但状态为 " + rnd.status + "（应为 sealed）");
      }
      return finish(failures);
    }

    if (!LotteryRules.isHex64(seed)) {
      fail("种子不是 64 位十六进制");
      return finish(failures);
    }
    var recalcCommit = LotteryRules.commitForSeed(seed);
    if (recalcCommit !== commitment) {
      fail("种子与承诺不匹配：重算 " + recalcCommit + "；包内承诺 " + commitment);
      return finish(failures);
    }
    pass("种子与承诺匹配：SHA256('COMMIT:'+seed) 与承诺一致");

    if (rnd.status !== "revealed") fail("包内有种子，但状态为 " + rnd.status + "（应为 revealed）");
    if (!(rnd.prizeCount > 0)) fail("中奖名额非法");
    if (rnd.prizeCount > entries.length) {
      fail("人数 " + entries.length + " 少于名额 " + rnd.prizeCount);
      return finish(failures);
    }
    if (dup.length) { fail("因名单重复，无法重算结果"); return finish(failures); }

    // 关键：独立重抽
    var recalc = LotteryRules.drawWinners(entries, seed, rnd.prizeCount);
    var stored = pkg.winners;
    if (!Array.isArray(stored)) {
      fail("核验包缺少 winners 数组");
    } else {
      if (stored.length !== rnd.prizeCount) fail("中奖数量 " + stored.length + " 不等于名额 " + rnd.prizeCount);
      if (hasDuplicates(stored)) fail("包内中奖编号存在重复");
      var outside = stored.filter(function (w) { return entries.indexOf(w) < 0; });
      if (outside.length) fail("包内中奖编号不在名单中：" + outside.join(", "));
      var same = stored.length === recalc.length && stored.every(function (w, i) { return w === recalc[i]; });
      if (!same) {
        var diffs = [];
        for (var i = 0; i < Math.max(stored.length, recalc.length); i++) {
          if (stored[i] !== recalc[i]) {
            diffs.push("第 " + (i + 1) + " 位：包内 " + stored[i] + "，重算 " + recalc[i]);
          }
        }
        fail("按公开规则由种子与名单重算的结果与包内结果不一致：\n" + diffs.slice(0, 20).join("\n"));
      } else {
        pass("按规则独立重算 " + rnd.prizeCount + " 个中奖编号，与包内结果完全一致");
      }
    }

    heading("独立重算的中奖编号（按产生顺序）");
    recalc.forEach(function (w, i) { info("  " + (i + 1) + ". " + w); });

    // ---- 弃权/递补与发放确认：从种子与名单独立重演（旧版包无字段，按无记录处理）----
    if (!("forfeits" in pkg) && !("currentWinners" in pkg)) {
      info("旧版核验包：无弃权字段，按无弃权处理。");
    } else {
      verifyPostReveal(pkg, entries, seed, rnd.prizeCount, recalc, pass, fail, info, heading);
    }
    return finish(failures);
  }

  // 揭晓后的全部追加记录（弃权/递补 + 奖品发放确认）统一重演：
  // 第 j 条弃权发生在“阶段 j→j+1”之间；发放确认带 forfeitsBefore，表示它
  // 发生在第 forfeitsBefore 次弃权之后。逐阶段先安置确认、再重演弃权，
  // 可识别伪造编号、错置的确认阶段、确认后的弃权。旧版包无确认字段按无确认处理。
  function verifyPostReveal(pkg, entries, seed, prizeCount, recalc, pass, fail, info, heading) {
    var forfeits = pkg.forfeits;
    if (!Array.isArray(forfeits)) {
      fail("forfeits 字段必须是数组");
      return;
    }
    for (var q = 0; q < forfeits.length; q++) {
      if (!forfeits[q] || forfeits[q].seq !== q) {
        fail("弃权记录 seq 不连续或缺失（应为 0,1,2,… 连续编号）");
        break;
      }
    }

    var total = prizeCount + forfeits.length;
    var extended = LotteryRules.drawWinners(entries, seed, Math.min(total, entries.length));

    // ---- 发放确认的结构校验与按阶段分组 ----
    var hasConfirmField = ("confirmations" in pkg);
    var confirmations = hasConfirmField ? pkg.confirmations : [];
    var byPhase = Object.create(null);
    var confirmValid = true;
    if (hasConfirmField) {
      if (!Array.isArray(confirmations) ||
          !confirmations.every(function (c) { return c && typeof c === "object"; })) {
        fail("confirmations 字段必须是对象数组");
        confirmValid = false;
      } else {
        var seenSlots = Object.create(null);
        confirmations.forEach(function (c) {
          var slot = c.slot, fb = c.forfeitsBefore, cEntry = c.entry;
          if (typeof slot !== "number" || slot !== (slot | 0) ||
              slot < 0 || slot >= prizeCount) {
            fail("发放确认席位非法：" + JSON.stringify(slot) +
                 "（应为 0 至 " + (prizeCount - 1) + " 的整数）");
            confirmValid = false;
            return;
          }
          if (seenSlots[slot]) {
            fail("席位 " + (slot + 1) + " 出现多条发放确认（每席位至多确认一次）");
            confirmValid = false;
            return;
          }
          seenSlots[slot] = 1;
          if (typeof fb !== "number" || fb !== (fb | 0) ||
              fb < 0 || fb > forfeits.length) {
            fail("席位 " + (slot + 1) + " 的发放确认 forfeitsBefore 非法：" +
                 JSON.stringify(fb) + "（应为 0 至弃权总次数 " + forfeits.length +
                 " 的整数，确认阶段被错置）");
            confirmValid = false;
            return;
          }
          if (typeof cEntry !== "string" || !cEntry) {
            fail("席位 " + (slot + 1) + " 的发放确认缺少 entry（确认编号）");
            confirmValid = false;
            return;
          }
          (byPhase[fb] = byPhase[fb] || []).push(c);
        });
      }
    }

    // ---- 逐阶段重演 ----
    var slots = recalc.slice();
    var locked = Object.create(null); // 已确认席位 slot -> entry

    function applyPhaseConfirmations(phase) {
      var list = byPhase[phase] || [];
      list.forEach(function (c) {
        var holder = slots[c.slot];
        if (holder === null || holder === undefined) {
          fail("席位 " + (c.slot + 1) + " 的发放确认（" + c.entry +
               "）无法重演：该阶段此席位为空席（空席不能确认，记录系伪造）");
          confirmValid = false;
          return;
        }
        if (c.entry !== holder) {
          fail("席位 " + (c.slot + 1) + " 的发放确认编号为 " + c.entry +
               "，但确认阶段（第 " + phase + " 次弃权之后）该席位的持有者是 " +
               holder + "（伪造编号或错置的确认阶段）");
          confirmValid = false;
          return;
        }
        locked[c.slot] = c.entry;
      });
    }

    for (var j = 0; j < forfeits.length; j++) {
      applyPhaseConfirmations(j);
      var f = forfeits[j];
      var expect = (prizeCount + j) < entries.length ? extended[prizeCount + j] : null;
      var fslot = slots.indexOf(f.entry);
      if (fslot < 0) {
        fail("第 " + (j + 1) + " 条弃权记录无法重演：编号 " + f.entry +
             " 当时不在有效席位中（弃权顺序被篡改或记录系伪造）");
        return;
      }
      if (String(fslot) in locked) {
        fail("第 " + (j + 1) + " 条弃权（席位 " + (fslot + 1) + "，编号 " + f.entry +
             "）发生在该席位奖品发放确认（" + locked[fslot] +
             "）之后：已确认席位不能再弃权（确认后的弃权系伪造）");
        return;
      }
      var rep = f.replacement === undefined ? null : f.replacement;
      if (rep !== expect) {
        fail("第 " + (j + 1) + " 条弃权（" + f.entry + "）的递补不符：包内为 " +
             (rep === null ? "（空）" : rep) + "，按规则应为 " +
             (expect === null ? "（无候补）" : expect) + "（伪造或跳过的递补）");
        return;
      }
      slots[fslot] = expect;
    }
    applyPhaseConfirmations(forfeits.length);

    if (forfeits.length) {
      pass(forfeits.length + " 条弃权/递补记录按规则从种子与名单重演，全部一致");
    } else {
      pass("无弃权记录");
    }
    if (hasConfirmField) {
      if (confirmations.length && confirmValid) {
        pass(confirmations.length +
             " 条奖品发放确认按弃权顺序重演确认当时的席位持有者，全部一致");
      } else if (!confirmations.length) {
        pass("无奖品发放确认记录");
      }
    } else {
      pass("旧版核验包：无发放确认字段，按无确认处理");
    }

    var current = pkg.currentWinners;
    if (!Array.isArray(current) || current.length !== prizeCount) {
      fail("currentWinners 缺失或不是与名额等长的数组");
    } else {
      var same = current.every(function (s, i) { return s === slots[i]; });
      if (!same) {
        var diffs = [];
        for (var i = 0; i < slots.length; i++) {
          if (current[i] !== slots[i]) {
            diffs.push("席位 " + (i + 1) + "：包内 " + current[i] + "，重演 " + slots[i]);
          }
        }
        fail("当前有效席位与重演结果不一致：\n" + diffs.slice(0, 20).join("\n"));
      } else {
        pass("当前有效席位（currentWinners）与弃权/确认重演结果一致");
      }
      // 已确认席位的当前持有者必须仍是确认时的编号
      Object.keys(locked).forEach(function (sStr) {
        var s = parseInt(sStr, 10);
        if (Array.isArray(current) && current[s] !== locked[s]) {
          fail("席位 " + (s + 1) + " 已确认发放给 " + locked[s] +
               "，但当前席位为 " + current[s] + "（确认后席位不应再变化）");
        }
      });
    }

    heading("弃权与递补记录（按操作顺序，已经规则重演核对）");
    if (!forfeits.length) info("（无）");
    forfeits.forEach(function (f) {
      info("  #" + (f.seq + 1) + " 弃权 " + f.entry + " → 递补 " +
           (f.replacement == null ? "（无候补，席位留空）" : f.replacement));
    });
    if (hasConfirmField && confirmations.length) {
      heading("奖品发放确认（已经弃权顺序重演确认当时的席位持有者）");
      confirmations.slice().sort(function (a, b) {
        return (typeof a.slot === "number" ? a.slot : -1) -
               (typeof b.slot === "number" ? b.slot : -1);
      }).forEach(function (c) {
        info("  席位 " + (c.slot + 1) + "  编号 " + c.entry +
             "（确认前弃权 " + c.forfeitsBefore + " 次）");
      });
    }
    heading("当前有效席位");
    slots.forEach(function (s, i) {
      info("  " + (i + 1) + ". " + (s === null ? "（空席）" : s) +
           (String(i) in locked ? "  ✅ 已确认发放" : ""));
    });
  }

  function finish(failures) {
    heading("结论");
    if (failures) {
      line("fail", "发现 " + failures + " 处不符，核验未通过。");
    } else {
      line("pass", "全部核验通过。中奖结果由公开规则、种子与封存名单确定性地推出。");
    }
  }

  $("pkgFile").addEventListener("change", function () {
    var file = this.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var pkg = JSON.parse(reader.result);
        verifyPackage(pkg);
      } catch (e) {
        out.innerHTML = "";
        line("fail", "无法解析 JSON：" + e.message + "（zip 请先解压，选择其中的 package.json）");
      }
    };
    reader.readAsText(file);
  });
})();
