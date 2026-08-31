/* 渲一份「没有球的 `预合成 3`」—— 用来把背景从帧序列里减干净。

   为什么需要：`预合成 3` 是不透明的，底下压着纯色 vignette + 两个调整图层，而调整图层的
   曝光带 4 个关键帧 —— **背景不是静态的**。所以「逐像素取全序列最小值当背景」减不干净：
   min 取到的是背景最暗那一刻，其余帧减完剩下的差值就是使用者会看到的那圈同心环。

   正解是逐帧减真背景。做法：把 `预合成 3` 里所有球的来源（`预合成 1` / `预合成 2` 实例）
   禁掉，另存成一个临时工程，让 aerender 渲那一份。**原工程不动、不保存** —— aerender 是
   独立进程读磁盘上的文件，所以必须另存，不能只在内存里改。

   用法：
     AfterFX.exe -r <本文件绝对路径>
     然后 aerender -project <临时工程> -comp "预合成 3" … 渲同样的帧范围 */
(function () {
  var AEP = 'D:\\Downloads\\Baidu\\17-siri按钮\\17-siri按钮文件夹\\ui设计工程文件.aep';
  var TMP = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-ref-ae\\bg-only.aep';
  var LOG = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-bg.txt';
  var lines = [];
  function W(s) { lines.push(String(s)); }

  try { app.beginSuppressDialogs(); } catch (e) {}
  try { app.open(new File(AEP)); } catch (e) { W('OPEN FAILED: ' + e.toString()); }

  var pr = app.project;
  var target = null;
  for (var i = 1; i <= pr.numItems; i++) {
    var it = pr.item(i);
    if (it instanceof CompItem && it.name === '预合成 3') { target = it; break; }
  }
  if (target === null) { W('预合成 3 not found'); }
  else {
    var off = 0;
    for (var L = 1; L <= target.numLayers; L++) {
      var ly = target.layer(L);
      var nm = '?';
      try { nm = ly.name; } catch (e) {}
      // 球的来源不只是那两个预合成 —— `白色 纯色 5` / `白色 纯色 6` 带关键帧（缩放 KEYS=2、
      // 不透明度 KEYS=6），那是**中心光核**，属于球。第一版只禁预合成，于是光核留在了背景里
      // 被当作背景减掉，六态渲出来中心全是一个黑洞（使用者一眼就会看到）。
      // 判据是「会不会随时间变」：会变的是内容，不变的才是背景。留下的只有 `深色 蓝色 纯色 2`
      // 与两个调整图层 —— 前者是底色，后者作用在它下面的层上，没有球时就是纯 vignette。
      var isOrb = (nm.indexOf('预合成 1') === 0)
        || (nm.indexOf('预合成 2') === 0)
        || (nm.indexOf('白色 纯色 5') === 0)
        || (nm.indexOf('白色 纯色 6') === 0);
      if (isOrb) {
        try { ly.enabled = false; off++; W('off\t' + nm); } catch (e) { W('cannot disable ' + nm); }
      } else {
        W('keep\t' + nm + '\tenabled=' + ly.enabled);
      }
    }
    W('disabled ' + off + ' orb layers');
    try {
      pr.save(new File(TMP));      // 另存，原文件不受影响
      W('saved\t' + TMP);
    } catch (e) { W('save failed: ' + e.toString()); }
  }

  var f = new File(LOG);
  f.encoding = 'UTF-8'; f.open('w'); f.write(lines.join('\n')); f.close();
  try { app.endSuppressDialogs(false); } catch (e) {}
  // 存完就退 —— aerender 与 AE GUI 实例同时在跑会让 aerender 报 `Unable to receive`
  try { app.quit(); } catch (e) {}
})();
