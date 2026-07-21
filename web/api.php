<?php
/**
 * CryptoMM Bot - 管理画面 API
 * ログイン / APIキー設定 / 売買ルール / 起動停止 / 価格取得 / 状態取得
 */
session_start();
header('Content-Type: application/json; charset=utf-8');

$ROOT     = dirname(__DIR__);
$ENV_FILE = $ROOT . '/.env';
$CFG_FILE = $ROOT . '/bot_config.json';
$STATE    = $ROOT . '/sim_state.json';
$PIDFILE  = $ROOT . '/bot.pid';

function load_cfg($f) {
    $def = [
        'username'         => 'admin',
        'password_hash'    => password_hash('crypto2026', PASSWORD_DEFAULT),
        'symbol'           => 'eth_jpy',
        'order_size'       => 0.05,
        'interval'         => 10,
        'spread_range'     => 300,
        'spread_trend'     => 800,
        'spread_threshold' => 300,
        'crash_threshold'  => 3,
    ];
    if (file_exists($f)) { $c = json_decode(file_get_contents($f), true); if (is_array($c)) return array_merge($def, $c); }
    file_put_contents($f, json_encode($def, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    return $def;
}
function save_cfg($f, $c) { file_put_contents($f, json_encode($c, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE)); }
function require_login() { if (empty($_SESSION['auth'])) { echo json_encode(['success'=>false,'need_login'=>true]); exit; } }
function keys_set($envf) {
    if (!file_exists($envf)) return false;
    $s = file_get_contents($envf);
    return preg_match('/BITBANK_API_KEY=.+/', $s) && preg_match('/BITBANK_API_SECRET=.+/', $s);
}
function http_get($url) {
    $ch = curl_init($url);
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER=>true, CURLOPT_TIMEOUT=>8, CURLOPT_CONNECTTIMEOUT=>5, CURLOPT_SSL_VERIFYPEER=>false]);
    $r = curl_exec($ch); curl_close($ch);
    return $r ? json_decode($r, true) : null;
}

$cfg    = load_cfg($CFG_FILE);
$action = $_REQUEST['action'] ?? 'status';

switch ($action) {

case 'login':
    $uOk = (($_POST['username'] ?? '') === ($cfg['username'] ?? 'admin'));
    $pOk = password_verify($_POST['password'] ?? '', $cfg['password_hash']);
    if ($uOk && $pOk) { $_SESSION['auth']=true; echo json_encode(['success'=>true]); }
    else echo json_encode(['success'=>false,'error'=>'IDまたはパスワードが違います']);
    break;

case 'logout':
    session_destroy(); echo json_encode(['success'=>true]); break;

case 'change_password':
    require_login();
    $new = $_POST['new_password'] ?? '';
    if (strlen($new) < 6) { echo json_encode(['success'=>false,'error'=>'6文字以上にしてください']); break; }
    if (!empty($_POST['new_username'])) $cfg['username'] = $_POST['new_username'];
    $cfg['password_hash'] = password_hash($new, PASSWORD_DEFAULT); save_cfg($CFG_FILE, $cfg);
    echo json_encode(['success'=>true]); break;

case 'get_config':
    require_login();
    echo json_encode(['success'=>true,'keys_set'=>keys_set($ENV_FILE),'username'=>$cfg['username'],'rules'=>[
        'symbol'=>$cfg['symbol'],'order_size'=>$cfg['order_size'],'interval'=>$cfg['interval'],
        'spread_range'=>$cfg['spread_range'],'spread_trend'=>$cfg['spread_trend'],
        'spread_threshold'=>$cfg['spread_threshold'],'crash_threshold'=>$cfg['crash_threshold'],
    ]], JSON_UNESCAPED_UNICODE); break;

case 'save_keys':
    require_login();
    $k = trim($_POST['api_key'] ?? ''); $s = trim($_POST['api_secret'] ?? '');
    if ($k==='' || $s==='') { echo json_encode(['success'=>false,'error'=>'両方入力してください']); break; }
    $env = file_exists($ENV_FILE) ? file_get_contents($ENV_FILE) : '';
    $env = preg_replace('/BITBANK_API_KEY=.*\n?/', '', $env);
    $env = preg_replace('/BITBANK_API_SECRET=.*\n?/', '', $env);
    $env = rtrim($env) . "\nBITBANK_API_KEY={$k}\nBITBANK_API_SECRET={$s}\n";
    file_put_contents($ENV_FILE, ltrim($env));
    echo json_encode(['success'=>true]); break;

case 'save_rules':
    require_login();
    foreach (['order_size','interval','spread_range','spread_trend','spread_threshold','crash_threshold'] as $key)
        if (isset($_POST[$key]) && $_POST[$key] !== '') $cfg[$key] = $_POST[$key] + 0;
    save_cfg($CFG_FILE, $cfg);
    echo json_encode(['success'=>true]); break;

case 'prices':
    // 公開APIで取得（APIキー不要）。GMOとbitbankのETH/JPY現在価格＋乖離
    $gmo = http_get('https://api.coin.z.com/public/v1/ticker?symbol=ETH_JPY');
    $bb  = http_get('https://public.bitbank.cc/eth_jpy/ticker');
    $gmoP = isset($gmo['data'][0]['last']) ? (float)$gmo['data'][0]['last'] : null;
    $bbP  = isset($bb['data']['last']) ? (float)$bb['data']['last'] : null;
    $gap  = ($gmoP !== null && $bbP !== null) ? abs($gmoP - $bbP) : null;
    echo json_encode(['success'=>true,'gmo'=>$gmoP,'bitbank'=>$bbP,'gap'=>$gap], JSON_UNESCAPED_UNICODE); break;

case 'start':
    require_login();
    $mode   = ($_POST['mode'] ?? 'sim') === 'live' ? 'live' : 'sim';
    $script = $mode === 'live' ? 'bot_v2.py' : 'demo.py';
    if (file_exists($PIDFILE)) @exec('kill ' . intval(file_get_contents($PIDFILE)) . ' 2>/dev/null');
    $log = $ROOT . '/bot.log';
    $cmd = sprintf('cd %s && nohup python3 %s > %s 2>&1 & echo $!', escapeshellarg($ROOT), escapeshellarg($script), escapeshellarg($log));
    $pid = trim(@shell_exec($cmd));
    if ($pid) file_put_contents($PIDFILE, $pid);
    echo json_encode(['success'=>true,'mode'=>$mode,'pid'=>$pid]); break;

case 'stop':
    require_login();
    if (file_exists($PIDFILE)) { @exec('kill ' . intval(file_get_contents($PIDFILE)) . ' 2>/dev/null'); @unlink($PIDFILE); }
    echo json_encode(['success'=>true]); break;

case 'status':
default:
    if (!file_exists($STATE)) { echo json_encode(['success'=>false,'error'=>'Bot未起動','auth'=>!empty($_SESSION['auth'])]); break; }
    $d = json_decode(file_get_contents($STATE), true) ?: [];
    $d['success'] = true;
    $d['running'] = (time() - ($d['updated_at'] ?? 0)) < 30;
    $d['auth']    = !empty($_SESSION['auth']);
    echo json_encode($d, JSON_UNESCAPED_UNICODE); break;
}
