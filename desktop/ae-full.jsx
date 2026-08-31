/* 把 `预合成 3` 里所有层的出点延长到合成末尾，好用上后面 21 秒的动作。

   为什么需要：六片的动作（`预合成 1`）有**整整 30 秒**，但 `预合成 3` 里引用它的那些层
   **出点在 8.7 秒**（第 208 帧），所以最终合成只用了前 8.7 秒 —— 我渲 0–287 帧时看到
   208 帧之后全黑，就是这个原因。而逐帧量下来，六片**转得最快的那一段在源帧 400–480**
   （角向亮度剖面 −61 度/秒，是前 8 秒那一段的 25 倍），那正是使用者说的思考态
   「很快速的一个过程」。它在工程里，只是没有被用进合成。

   所以这一步不是「找素材」，是**拿着工程做一处修改**：把出点拉到 30 秒，辉光链保持完整
   （五层高斯 + Deep Glow 都在 `预合成 3` 里），只是时间往后取。关键帧只在前 8.7 秒有值的
   属性会保持最后一个关键帧 —— 可接受，我们要的是那一段的**运动**。

   用法：AfterFX.exe -r <本文件>，然后渲 `full.aep` 的 `合成 1`。 */
(function () {
  var AEP = 'D:\\Downloads\\Baidu\\17-siri按钮\\17-siri按钮文件夹\\ui设计工程文件.aep';
  var TMP = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-ref-ae\\full.aep';
  var BGT = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-ref-ae\\full-bg.aep';
  var LOG = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-full.txt';
  var lines = [];
  function W(s) { lines.push(String(s)); }

  try { app.beginSuppressDialogs(); } catch (e) {}
  try { app.open(new File(AEP)); } catch (e) { W('OPEN FAILED: ' + e.toString()); }
  var pr = app.project;

  function find(nm) {
    for (var i = 1; i <= pr.numItems; i++) {
      var it = pr.item(i);
      if (it instanceof CompItem && it.name === nm) return it;
    }
    return null;
  }

  var p3 = find('预合成 3');
  if (p3 === null) { W('预合成 3 not found'); }
  else {
    var n = 0;
    for (var L = 1; L <= p3.numLayers; L++) {
      var ly = p3.layer(L);
      var nm = '?';
      try { nm = ly.name; } catch (e) {}
      try {
        var before = ly.outPoint;
        // 先放开源素材的可用长度限制，再把出点推到合成末尾
        if (ly.outPoint < p3.duration - 1e-6) {
          ly.outPoint = p3.duration;
          n++;
          W('extend\t' + nm + '\t' + before.toFixed(3) + ' -> ' + ly.outPoint.toFixed(3));
        } else {
          W('keep\t' + nm + '\tout=' + before.toFixed(3));
        }
      } catch (e2) { W('cannot extend ' + nm + ': ' + e2.toString()); }
    }
    W('extended ' + n + ' layers');
    try { pr.save(new File(TMP)); W('saved\t' + TMP); }
    catch (e3) { W('save failed: ' + e3.toString()); }

    // 同一份工程再存一版「球层全禁」的，供逐帧减背景用 —— 两份必须来自同一次延长，
    // 否则前景与背景的层时间不一致，减出来是错的。
    var off = 0;
    for (var M = 1; M <= p3.numLayers; M++) {
      var l2 = p3.layer(M);
      var n2 = '?';
      try { n2 = l2.name; } catch (e) {}
      var isOrb = (n2.indexOf('预合成 1') === 0) || (n2.indexOf('预合成 2') === 0)
        || (n2.indexOf('白色 纯色 5') === 0) || (n2.indexOf('白色 纯色 6') === 0);
      if (isOrb) { try { l2.enabled = false; off++; } catch (e) {} }
    }
    W('bg: disabled ' + off + ' orb layers');
    try { pr.save(new File(BGT)); W('saved\t' + BGT); }
    catch (e4) { W('bg save failed: ' + e4.toString()); }
  }

  var f = new File(LOG);
  f.encoding = 'UTF-8'; f.open('w'); f.write(lines.join('\n')); f.close();
  try { app.endSuppressDialogs(false); } catch (e) {}
  try { app.quit(); } catch (e) {}
})();
