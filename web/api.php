<?php
/**
 * CryptoMM Bot - 管理画面 API
 * ログイン(初回設定) / APIキー設定 / 売買ルール / 起動停止 / 価格取得 / 状態取得
 * セキュリティ: CSRFトークン / ログインrate limit / session固定化対策 / status認証必須
 */
$isHttps = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
        || (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https');
session_set_cookie_params(['httponly' => true, 'samesite' => 'Lax', 'secure' => $isHttps, 'path' => '/']);
session_start();
header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');

$ROOT     = dirname(__DIR__);
$ENV_FILE = $ROOT . '/.env';
$CFG_FILE = $ROOT . '/bot_config.json';
$STATE    = $ROOT . '/sim_state.json';
$PIDFILE  = $ROOT . '/bot.pid';
$PRICE_CACHE = $ROOT . '/price_cache.json';
$RL_FILE  = $ROOT . '/login_attempts.json';

// Bot側(bot_v2.py CONFIG_LIMITS)と同じ厳格な上下限
$LIMITS = [
    'order_size'        => [0.001, 1.0],
    'interval'          => [3, 300],
    'spread_range'      => [10, 100000],
    'spread_trend'      => [10, 100000],
    'spread_threshold'  => [0, 100000],
    'crash_threshold'   => [0.1, 50.0],
    'stop_loss_rate'    => [0.001, 0.5],
    'max_position'      => [0.001, 10.0],
    'daily_loss_limit'  => [100, 10000000],
    'max_order_jpy'     => [100, 10000000],
    'max_open_orders'   => [1, 20],
];
$SYMBOLS = ['eth_jpy','btc_jpy','xrp_jpy','ltc_jpy','mona_jpy','bcc_jpy'];

function jout($arr) { echo json_encode($arr, JSON_UNESCAPED_UNICODE); exit; }

function load_cfg($f) {
    // 固定の初期パスワードは廃止。password_hash が空 = 初回設定が必要
    $def = [
        'username'         => 'admin',
        'password_hash'    => '',
        'symbol'           => 'eth_jpy',
        'order_size'       => 0.05,
        'interval'         => 10,
        'spread_range'     => 300,
        'spread_trend'     => 800,
        'spread_threshold' => 300,
        'crash_threshold'  => 3,
        'stop_loss_rate'   => 0.02,
        'max_position'     => 0.2,
        'daily_loss_limit' => 10000,
        'max_order_jpy'    => 100000,
        'max_open_orders'  => 4,
        'post_only'        => true,
        'live_confirmed'   => false,
    ];
    if (file_exists($f)) { $c = json_decode(file_get_contents($f), true); if (is_array($c)) return array_merge($def, $c); }
    save_cfg($f, $def);
    return $def;
}
function save_cfg($f, $c) {
    file_put_contents($f, json_encode($c, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    @chmod($f, 0600);
}
function require_login() { if (empty($_SESSION['auth'])) jout(['success'=>false,'need_login'=>true,'error'=>'ログインが必要です']); }
function require_csrf() {
    $t = $_POST['csrf'] ?? '';
    if (empty($_SESSION['csrf']) || !is_string($t) || !hash_equals($_SESSION['csrf'], $t))
        jout(['success'=>false,'error'=>'CSRFトークンが不正です。画面を再読み込みしてください']);
}
function keys_set($envf) {
    if (!file_exists($envf)) return false;
    $s = file_get_contents($envf);
    return preg_match('/BITBANK_API_KEY=\S+/', $s) && preg_match('/BITBANK_API_SECRET=\S+/', $s);
}
function http_get($url) {
    $ch = curl_init($url);
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER=>true, CURLOPT_TIMEOUT=>8, CURLOPT_CONNECTTIMEOUT=>5, CURLOPT_SSL_VERIFYPEER=>true, CURLOPT_SSL_VERIFYHOST=>2]);
    $r = curl_exec($ch); $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE); curl_close($ch);
    if ($r === false || $code !== 200) return null;
    return json_decode($r, true);
}

// --- ログインrate limit（5回失敗で5分ロック） ---
function rl_locked($f) {
    if (!file_exists($f)) return 0;
    $d = json_decode(file_get_contents($f), true) ?: [];
    $left = ($d['locked_until'] ?? 0) - time();
    return max(0, $left);
}
function rl_fail($f) {
    $d = file_exists($f) ? (json_decode(file_get_contents($f), true) ?: []) : [];
    $d['fails'] = ($d['fails'] ?? 0) + 1;
    if ($d['fails'] >= 5) { $d['locked_until'] = time() + 300; $d['fails'] = 0; }
    file_put_contents($f, json_encode($d)); @chmod($f, 0600);
}
function rl_clear($f) { @unlink($f); }

function login_ok_session() {
    session_regenerate_id(true);
    $_SESSION['auth'] = true;
    $_SESSION['csrf'] = bin2hex(random_bytes(16));
}

$cfg    = load_cfg($CFG_FILE);
$action = $_REQUEST['action'] ?? 'status';
$needSetup = empty($cfg['password_hash']);

switch ($action) {

case 'setup_check':
    // 初回設定が必要かどうかだけ返す（ログイン不要）
    jout(['success'=>true,'need_setup'=>$needSetup]);

case 'setup':
    // 初回パスワード設定（password_hash が空の時だけ1回だけ使える）
    if (!$needSetup) jout(['success'=>false,'error'=>'初回設定は完了済みです']);
    $u = trim($_POST['username'] ?? 'admin');
    $p = $_POST['password'] ?? '';
    if ($u === '' || !preg_match('/^[a-zA-Z0-9_-]{1,32}$/', $u)) jout(['success'=>false,'error'=>'IDは半角英数32文字以内にしてください']);
    if (strlen($p) < 8) jout(['success'=>false,'error'=>'パスワードは8文字以上にしてください']);
    $cfg['username'] = $u;
    $cfg['password_hash'] = password_hash($p, PASSWORD_DEFAULT);
    save_cfg($CFG_FILE, $cfg);
    login_ok_session();
    rl_clear($RL_FILE);
    jout(['success'=>true,'csrf'=>$_SESSION['csrf']]);

case 'login':
    if (($left = rl_locked($RL_FILE)) > 0) jout(['success'=>false,'error'=>"ログイン試行が多すぎます。{$left}秒後に再試行してください"]);
    $uOk = hash_equals((string)($cfg['username'] ?? 'admin'), (string)($_POST['username'] ?? ''));
    $pOk = !$needSetup && password_verify($_POST['password'] ?? '', $cfg['password_hash']);
    if ($uOk && $pOk) {
        login_ok_session();
        rl_clear($RL_FILE);
        jout(['success'=>true,'csrf'=>$_SESSION['csrf']]);
    }
    rl_fail($RL_FILE);
    jout(['success'=>false,'error'=>$needSetup ? '初回設定を行ってください' : 'IDまたはパスワードが違います']);

case 'logout':
    $_SESSION = [];
    session_destroy();
    jout(['success'=>true]);
}

// 以降は全てログイン必須
require_login();
// GET以外（状態を変える操作）はCSRFトークン必須
if ($_SERVER['REQUEST_METHOD'] === 'POST') require_csrf();

switch ($action) {

case 'get_config':
    jout(['success'=>true,'csrf'=>$_SESSION['csrf'],'keys_set'=>keys_set($ENV_FILE),'username'=>$cfg['username'],
        'live_confirmed'=>!empty($cfg['live_confirmed']),'rules'=>[
        'symbol'=>$cfg['symbol'],'order_size'=>$cfg['order_size'],'interval'=>$cfg['interval'],
        'spread_range'=>$cfg['spread_range'],'spread_trend'=>$cfg['spread_trend'],
        'spread_threshold'=>$cfg['spread_threshold'],'crash_threshold'=>$cfg['crash_threshold'],
        'stop_loss_rate'=>$cfg['stop_loss_rate'],'max_position'=>$cfg['max_position'],
        'daily_loss_limit'=>$cfg['daily_loss_limit'],'max_order_jpy'=>$cfg['max_order_jpy'],
        'max_open_orders'=>$cfg['max_open_orders'],'post_only'=>!empty($cfg['post_only']),
    ]]);

case 'change_password':
    $new = $_POST['new_password'] ?? '';
    if (strlen($new) < 8) jout(['success'=>false,'error'=>'8文字以上にしてください']);
    if (!empty($_POST['new_username'])) {
        $nu = trim($_POST['new_username']);
        if (!preg_match('/^[a-zA-Z0-9_-]{1,32}$/', $nu)) jout(['success'=>false,'error'=>'IDは半角英数32文字以内にしてください']);
        $cfg['username'] = $nu;
    }
    $cfg['password_hash'] = password_hash($new, PASSWORD_DEFAULT);
    save_cfg($CFG_FILE, $cfg);
    jout(['success'=>true]);

case 'save_keys':
    $k = trim($_POST['api_key'] ?? ''); $s = trim($_POST['api_secret'] ?? '');
    if ($k==='' || $s==='') jout(['success'=>false,'error'=>'両方入力してください']);
    $env = file_exists($ENV_FILE) ? file_get_contents($ENV_FILE) : '';
    $env = preg_replace('/BITBANK_API_KEY=.*\R?/', '', $env);
    $env = preg_replace('/BITBANK_API_SECRET=.*\R?/', '', $env);
    $env = rtrim($env) . "\nBITBANK_API_KEY={$k}\nBITBANK_API_SECRET={$s}\n";
    file_put_contents($ENV_FILE, ltrim($env));
    @chmod($ENV_FILE, 0600);
    jout(['success'=>true]);

case 'save_rules':
    if (isset($_POST['symbol']) && $_POST['symbol'] !== '') {
        if (!in_array($_POST['symbol'], $SYMBOLS, true)) jout(['success'=>false,'error'=>'symbolが不正です']);
        $cfg['symbol'] = $_POST['symbol'];
    }
    foreach ($LIMITS as $key => [$lo, $hi]) {
        if (!isset($_POST[$key]) || $_POST[$key] === '') continue;
        if (!is_numeric($_POST[$key])) jout(['success'=>false,'error'=>"{$key} は数値で入力してください"]);
        $v = $_POST[$key] + 0;
        if ($v < $lo || $v > $hi) jout(['success'=>false,'error'=>"{$key} は {$lo} 〜 {$hi} の範囲で入力してください"]);
        $cfg[$key] = $v;
    }
    $cfg['post_only'] = !empty($_POST['post_only']) && $_POST['post_only'] !== '0';
    save_cfg($CFG_FILE, $cfg);
    jout(['success'=>true]);

case 'confirm_live':
    // 本番モードの明示確認。APIキー未設定なら立てられない
    if (!keys_set($ENV_FILE)) jout(['success'=>false,'error'=>'先にAPIキーを登録してください']);
    $cfg['live_confirmed'] = ($_POST['enable'] ?? '') === '1';
    save_cfg($CFG_FILE, $cfg);
    jout(['success'=>true,'live_confirmed'=>$cfg['live_confirmed']]);

case 'prices':
    // 公開APIで取得（APIキー不要）。5秒キャッシュでrate制限
    if (file_exists($PRICE_CACHE)) {
        $c = json_decode(file_get_contents($PRICE_CACHE), true);
        if ($c && (time() - ($c['ts'] ?? 0)) < 5) jout($c['data'] + ['success'=>true,'cached'=>true]);
    }
    $gmo = http_get('https://api.coin.z.com/public/v1/ticker?symbol=ETH_JPY');
    $bb  = http_get('https://public.bitbank.cc/eth_jpy/ticker');
    $gmoP = isset($gmo['data'][0]['last']) ? (float)$gmo['data'][0]['last'] : null;
    $bbP  = isset($bb['data']['last']) ? (float)$bb['data']['last'] : null;
    $gap  = ($gmoP !== null && $bbP !== null) ? abs($gmoP - $bbP) : null;
    $data = ['gmo'=>$gmoP,'bitbank'=>$bbP,'gap'=>$gap];
    file_put_contents($PRICE_CACHE, json_encode(['ts'=>time(),'data'=>$data]));
    @chmod($PRICE_CACHE, 0600);
    jout($data + ['success'=>true]);

case 'start':
    $mode = ($_POST['mode'] ?? 'sim') === 'live' ? 'live' : 'sim';
    if ($mode === 'live') {
        // live起動条件: APIキー + 明示確認(live_confirmed) + 起動のたびの確認(confirm=LIVE)
        if (!keys_set($ENV_FILE)) jout(['success'=>false,'error'=>'APIキーが未設定です。先に「APIキー設定」で登録してください']);
        if (empty($cfg['live_confirmed'])) jout(['success'=>false,'error'=>'本番モードの明示確認がされていません。確認チェックをONにしてください']);
        if (($_POST['confirm'] ?? '') !== 'LIVE') jout(['success'=>false,'error'=>'本番起動には確認入力が必要です']);
        foreach (['max_position','daily_loss_limit','max_order_jpy','max_open_orders','stop_loss_rate'] as $k) {
            if (!isset($cfg[$k]) || !is_numeric($cfg[$k])) jout(['success'=>false,'error'=>"安全設定 {$k} が未設定のため起動できません"]);
            [$lo,$hi] = $LIMITS[$k];
            if ($cfg[$k] < $lo || $cfg[$k] > $hi) jout(['success'=>false,'error'=>"安全設定 {$k} が範囲外のため起動できません"]);
        }
    }
    if (file_exists($PIDFILE)) { @exec('kill ' . intval(file_get_contents($PIDFILE)) . ' 2>/dev/null'); @unlink($PIDFILE); usleep(300000); }
    $log = $ROOT . '/bot.log';
    $cmd = sprintf('cd %s && nohup python3 bot_v2.py --mode %s > %s 2>&1 & echo $!',
        escapeshellarg($ROOT), escapeshellarg($mode), escapeshellarg($log));
    $pid = trim(@shell_exec($cmd));
    if (!$pid || !ctype_digit($pid)) jout(['success'=>false,'error'=>'Botプロセスの起動に失敗しました']);
    file_put_contents($PIDFILE, $pid);
    @chmod($PIDFILE, 0600);
    jout(['success'=>true,'mode'=>$mode,'pid'=>$pid]);

case 'stop':
    if (file_exists($PIDFILE)) {
        $pid = intval(file_get_contents($PIDFILE));
        if ($pid > 0) @exec('kill ' . $pid . ' 2>/dev/null');  // SIGTERM（Bot側で自注文を取消して終了）
        @unlink($PIDFILE);
    }
    jout(['success'=>true]);

case 'status':
default:
    // 状態はログイン必須（上の require_login 済み）
    if (!file_exists($STATE)) jout(['success'=>false,'error'=>'Bot未起動','auth'=>true]);
    $d = json_decode(file_get_contents($STATE), true) ?: [];
    $d['success'] = true;
    $d['running'] = (time() - ($d['updated_at'] ?? 0)) < 30 && empty($d['halted']);
    $d['auth']    = true;
    jout($d);
}
