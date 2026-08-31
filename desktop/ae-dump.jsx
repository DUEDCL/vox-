/* AE 工程参数 dump —— 直接对接本机 AE 的那一半。
   用法（我自己跑，不需要你动手）：
     "C:\Program Files\Adobe\Adobe After Effects 2023\Support Files\AfterFX.exe" -r <本文件绝对路径>

   它做三件事，全部只读：
     ① 打开工程（原文件不改，也不保存）
     ② 把每个合成的尺寸/帧率/时长、每层的效果链与**全部参数值**（含 Element 3D 的
        Deform / 材质 / 灯光 / 雾）、摄像机设置、表达式，dump 成一份扁平文本
     ③ 顺便列出可用的**输出模块模板名**（中文 AE 的名字猜不出来，而 aerender 要它才能
        导 PNG 序列）

   ExtendScript 是 ES3：不能用 let / const / 箭头函数 / 模板字符串，也没有内置 JSON。
   所以下面全是 var + 字符串拼接，而且每个属性访问都包 try —— 第三方效果的某些参数
   读一下就抛异常，一个没包住整份 dump 就断在那里。 */
(function () {
  var AEP = 'D:\\Downloads\\Baidu\\17-siri按钮\\17-siri按钮文件夹\\ui设计工程文件.aep';
  var OUT = 'D:\\program\\vioce-wake\\.claude\\worktrees\\ui-d91f92\\.vox-ae-dump.txt';
  var lines = [];
  function W(s) { lines.push(s); }

  function fmt(v) {
    if (v === null || v === undefined) return '';
    if (v instanceof Array) {
      var a = [];
      for (var i = 0; i < v.length; i++) a.push(typeof v[i] === 'number' ? Math.round(v[i] * 1e4) / 1e4 : String(v[i]));
      return '[' + a.join(', ') + ']';
    }
    if (typeof v === 'number') return String(Math.round(v * 1e4) / 1e4);
    return String(v);
  }

  /** 递归 dump 一个属性组。`depth` 限制避免 E3D 的深层组把文件撑爆。 */
  function dumpGroup(g, path, depth) {
    if (depth > 6) return;
    var n = 0;
    try { n = g.numProperties; } catch (e) { return; }
    for (var i = 1; i <= n; i++) {
      var p = null;
      try { p = g.property(i); } catch (e) { continue; }
      if (p === null) continue;
      var nm = '';
      try { nm = p.name; } catch (e) { nm = '?'; }
      var full = path + '/' + nm;
      var isGroup = false;
      try { isGroup = (p.propertyType === PropertyType.INDEXED_GROUP || p.propertyType === PropertyType.NAMED_GROUP); } catch (e) {}
      if (isGroup) {
        dumpGroup(p, full, depth + 1);
        continue;
      }
      var val = '', exp = '', keys = 0;
      try { val = fmt(p.value); } catch (e) { val = '<n/a>'; }
      try { exp = p.expressionEnabled ? p.expression : ''; } catch (e) {}
      try { keys = p.numKeys; } catch (e) {}
      // 只记有信息量的：值非 0 / 有表达式 / 有关键帧
      if (val === '0' && exp === '' && keys === 0) continue;
      var row = full + '\t' + val;
      if (keys > 0) row += '\tKEYS=' + keys;
      if (exp !== '') row += '\tEXPR=' + exp.replace(/[\r\n]+/g, ' ');
      W(row);
    }
  }

  try { app.beginSuppressDialogs(); } catch (e) {}
  try {
    app.open(new File(AEP));
  } catch (e) {
    W('OPEN FAILED: ' + e.toString());
  }

  var pr = app.project;
  W('=== 工程 ===');
  try { W('items\t' + pr.numItems); } catch (e) {}

  // 输出模块模板名 —— aerender 导 PNG 序列要用它
  W('');
  W('=== 输出模块模板 ===');
  try {
    var firstComp = null;
    for (var i = 1; i <= pr.numItems; i++) {
      if (pr.item(i) instanceof CompItem) { firstComp = pr.item(i); break; }
    }
    if (firstComp !== null) {
      var rqi = pr.renderQueue.items.add(firstComp);
      var om = rqi.outputModule(1);
      var t = om.templates;
      for (var k = 0; k < t.length; k++) W('OM\t' + t[k]);
      var rt = rqi.templates;
      for (var k2 = 0; k2 < rt.length; k2++) W('RS\t' + rt[k2]);
      rqi.remove();
    }
  } catch (e) { W('templates failed: ' + e.toString()); }

  W('');
  W('=== 合成 ===');
  for (var i2 = 1; i2 <= pr.numItems; i2++) {
    var it = pr.item(i2);
    if (!(it instanceof CompItem)) continue;
    W('COMP\t' + it.name + '\t' + it.width + 'x' + it.height
      + '\tfps=' + it.frameRate + '\tdur=' + Math.round(it.duration * 1e3) / 1e3 + 's'
      + '\tlayers=' + it.numLayers);
    for (var L = 1; L <= it.numLayers; L++) {
      var ly = it.layer(L);
      var kind = 'Layer';
      try {
        if (ly instanceof CameraLayer) kind = 'CAMERA';
        else if (ly instanceof LightLayer) kind = 'LIGHT';
        else if (ly instanceof TextLayer) kind = 'TEXT';
        else if (ly instanceof ShapeLayer) kind = 'SHAPE';
      } catch (e) {}
      var bm = '';
      try { bm = String(ly.blendingMode); } catch (e) {}
      W('  LAYER\t' + kind + '\t' + ly.name + '\tenabled=' + ly.enabled + '\tblend=' + bm
        + '\t3d=' + (ly.threeDLayer === true));
      // Transform
      try { dumpGroup(ly.property('ADBE Transform Group'), '  ' + ly.name + '/Transform', 4); } catch (e) {}
      // 摄像机 / 灯光的选项
      try { dumpGroup(ly.property('ADBE Camera Options Group'), '  ' + ly.name + '/CameraOptions', 4); } catch (e) {}
      try { dumpGroup(ly.property('ADBE Light Options Group'), '  ' + ly.name + '/LightOptions', 4); } catch (e) {}
      // 效果链
      try {
        var fx = ly.property('ADBE Effect Parade');
        if (fx !== null) {
          for (var E = 1; E <= fx.numProperties; E++) {
            var ef = fx.property(E);
            W('    FX\t' + ef.name + '\t' + ef.matchName);
            dumpGroup(ef, '    ' + ly.name + '/' + ef.name, 3);
          }
        }
      } catch (e) {}
    }
  }

  var f = new File(OUT);
  f.encoding = 'UTF-8';
  f.open('w');
  f.write(lines.join('\n'));
  f.close();
  try { app.endSuppressDialogs(false); } catch (e) {}
})();
