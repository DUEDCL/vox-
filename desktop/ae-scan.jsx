/* 把 30 秒的工程压成 3 秒 —— 用来**看全**，不是用来出资产。

   为什么需要：工程里每个合成都是 **30 秒 / 720 帧**，而我之前只渲了 0–287 帧（12 秒），
   并据此断言「工程里没有六个圆点绕球心公转的那一相」。使用者说「我给出的工程文件里就有
   对应思考的这一表现」—— 那一相很可能就在我没看的那 18 秒里。720 帧 E3D 全渲要十几分钟
   且吃 7 GB 磁盘，所以先抽样：把 `合成 1` 装进一个 3 秒的合成、用 **时间重映射**（不是
   时间伸缩）压 10 倍。

   为什么必须用 timeRemap 而不是 `layer.stretch`：`预合成 1` 里六片的旋转全是表达式
   （`time*80`、`time*45+value`…），而表达式读的是**层时间**。`stretch` 压缩层时间 ⇒
   表达式的 time 跟着变小 ⇒ 动作变慢，等于换了一段动画。timeRemap 把源时间原样映射到
   新时间轴，表达式仍按源时间求值 ⇒ 拿到的就是每 10 帧抽 1 帧。

   用法：AfterFX.exe -r <本文件>，然后渲合成 `scanF` 的 0–71。 */
(function () {
  var AEP = 'D:\\Downloads\\Baidu\\17-siri按钮\\17-siri按钮文件夹\\ui设计工程文件.aep';
  var TMP = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-ref-ae\\scan.aep';
  var LOG = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-scan.txt';
  var SRC = '合成 1';        // 最终合成（含全部后期），30s
  var OUT_DUR = 3.0;         // 压成 3 秒 ⇒ 72 帧覆盖 720 帧
  var lines = [];
  function W(s) { lines.push(String(s)); }

  try { app.beginSuppressDialogs(); } catch (e) {}
  try { app.open(new File(AEP)); } catch (e) { W('OPEN FAILED: ' + e.toString()); }

  var pr = app.project;
  var src = null;
  for (var i = 1; i <= pr.numItems; i++) {
    var it = pr.item(i);
    if (it instanceof CompItem && it.name === SRC) { src = it; break; }
  }
  if (src === null) { W(SRC + ' not found'); }
  else {
    W('src\t' + src.name + '\t' + src.width + 'x' + src.height + '\tdur=' + src.duration);
    var scan = pr.items.addComp('scanF', src.width, src.height, 1, OUT_DUR, src.frameRate);
    var ly = scan.layers.add(src);
    try {
      ly.timeRemapEnabled = true;
      var tr = ly.property('ADBE Time Remapping');
      // timeRemap 默认两个关键帧：(0 → 0) 与 (dur → dur)。把第二个搬到 OUT_DUR 处，
      // 值仍是源的 dur ⇒ 3 秒里线性走完 30 秒。
      while (tr.numKeys > 2) tr.removeKey(tr.numKeys);
      tr.setValueAtTime(0, 0);
      tr.setValueAtTime(OUT_DUR, src.duration - 1 / src.frameRate);
      while (tr.numKeys > 2) tr.removeKey(2);
      for (var k = 1; k <= tr.numKeys; k++) {
        tr.setInterpolationTypeAtKey(k, KeyframeInterpolationType.LINEAR, KeyframeInterpolationType.LINEAR);
      }
      ly.outPoint = OUT_DUR;
      W('remap\tkeys=' + tr.numKeys + '\tt0=' + tr.keyTime(1) + '->' + tr.keyValue(1)
        + '\tt1=' + tr.keyTime(tr.numKeys) + '->' + tr.keyValue(tr.numKeys));
    } catch (e) { W('remap failed: ' + e.toString()); }
    try { pr.save(new File(TMP)); W('saved\t' + TMP); }
    catch (e2) { W('save failed: ' + e2.toString()); }
  }

  var f = new File(LOG);
  f.encoding = 'UTF-8'; f.open('w'); f.write(lines.join('\n')); f.close();
  try { app.endSuppressDialogs(false); } catch (e) {}
  try { app.quit(); } catch (e) {}
})();
