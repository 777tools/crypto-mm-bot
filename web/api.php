<?php
/**
 * CryptoMM Bot - 管理画面 API
 * ログイン(初回設定+setup token) / APIキー設定 / 売買ルール / 起動停止 / 価格取得 / 状態取得
 * セキュリティ: CSRFトークン / IP別ログインrate limit(flock) / session固定化対策 /
 *               status認証必須 / PID実体確認・二重起動lock / setup token
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
$SETUP_TOKEN_FILE = $ROOT . '/setup_token.txt';
$LOCK_FILE = $ROOT . '/start.lock';

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
// ETH/JPY のみ（GMO価格・残高取得がETH固定のため他ペアは安全装置が無効になる）
$SYMBOLS = ['eth_jpy'];

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

// --- IP別ログインrate limit（flockで原子的に。5回失敗で5分ロック） ---
function rl_with_lock($f, $fn) {
    $fp = fopen($f, 'c+');
    if (!$fp) return $fn([]);
    flock($fp, LOCK_EX);
    $raw = stream_get_contents($fp);
    $data = $raw ? (json_decode($raw, true) ?: []) : [];
    $result = $fn($data);
    if ($result[1] !== null) {  // 書き戻すデータ
        rewind($fp); ftruncate($fp, 0); fwrite($fp, json_encode($result[1])); fflush($fp);
    }
    flock($fp, LOCK_UN); fclose($fp);
    @chmod($f, 0600);
    return $result[0];
}
function rl_check($f, $ip) {
    return rl_with_lock($f, function($d) use ($ip) {
        $left = (($d[$ip]['locked_until'] ?? 0) - time());
        return [max(0, $left), null];  // 読むだけ
    });
}
function rl_fail($f, $ip) {
    return rl_with_lock($f, function($d) use ($ip) {
        $d[$ip]['fails'] = ($d[$ip]['fails'] ?? 0) + 1;
        if ($d[$ip]['fails'] >= 5) { $d[$ip]['locked_until'] = time() + 300; $d[$ip]['fails'] = 0; }
        return [null, $d];
    });
}
function rl_clear($f, $ip) {
    rl_with_lock($f, function($d) use ($ip) { unset($d[$ip]); return [null, $d]; });
}

function login_ok_session() {
    session_regenerate_id(true);
    $_SESSION['auth'] = true;
    $_SESSION['csrf'] = bin2hex(random_bytes(16));
}

// --- setup token（初回設定の先着乗っ取り防止。サーバ上のファイルを読める人だけ設定可能） ---
function ensure_setup_token($f) {
    if (!file_exists($f)) {
        file_put_contents($f, bin2hex(random_bytes(16)) . "\n");
        @chmod($f, 0600);
    }
}
function setup_token_ok($f, $t) {
    if (!file_exists($f)) return false;
    $real = trim((string)file_get_contents($f));
    return $real !== '' && is_string($t) && hash_equals($real, trim($t));
}

// --- PID管理（実体が自Botか確認してから操作） ---
function pid_bot_args($pid) {
    if ($pid <= 0) return null;
    $args = trim((string)@shell_exec('ps -p ' . intval($pid) . ' -o args= 2>/dev/null'));
    return $args === '' ? null : $args;
}
function pid_is_our_bot($pid) {
    $args = pid_bot_args($pid);
    return $args !== null && strpos($args, 'bot_v2.py') !== false;
}
function stop_bot_and_wait($pidfile, $timeoutSec = 20) {
    // 戻り値: [bool成功, stringメッセージ]
    if (!file_exists($pidfile)) return [true, '停止済み'];
    $pid = intval(file_get_contents($pidfile));
    if ($pid <= 0) { @unlink($pidfile); return [true, '停止済み']; }
    if (!pid_is_our_bot($pid)) {
        // 自分のBotではないプロセスは絶対にkillしない
        @unlink($pidfile);
        return [true, 'PIDファイルを整理しました（実プロセスは自Botではありませんでした）'];
    }
    @exec('kill ' . intval($pid) . ' 2>/dev/null');  // SIGTERM（Bot側で自注文を取消して終了）
    $t = 0;
    while ($t < $timeoutSec) {
        if (pid_bot_args($pid) === null) { @unlink($pidfile); return [true, '停止しました（自Bot注文は取消済み）']; }
        usleep(500000); $t += 0.5;
    }
    return [false, "停止タイムアウト（{$timeoutSec}秒）。自Bot注文の取消が未完の可能性があります。取引所画面で確認してください"];
}

$cfg    = load_cfg($CFG_FILE);
$action = $_REQUEST['action'] ?? 'status';
$needSetup = empty($cfg['password_hash']);
// live_confirmed は厳格に true のみ有効（文字列 "false" 等を true 扱いしない）
$cfg['live_confirmed'] = ($cfg['live_confirmed'] === true);
$clientIp = $_SERVER['REMOTE_ADDR'] ?? 'unknown';

if ($needSetup) ensure_setup_token($SETUP_TOKEN_FILE);

switch ($action) {

case 'setup_check':
    // 初回設定が必要かどうかだけ返す（ログイン不要）
    jout(['success'=>true,'need_setup'=>$needSetup]);

case 'setup':
    // 初回パスワード設定（password_hash が空の時だけ1回だけ使える + setup token 必須）
    if (($left = rl_check($RL_FILE, $clientIp)) > 0) jout(['success'=>false,'error'=>"試行が多すぎます。{$left}秒後に再試行してください"]);
    if (!$needSetup) jout(['success'=>false,'error'=>'初回設定は完了済みです']);
    if (!setup_token_ok($SETUP_TOKEN_FILE, $_POST['setup_token'] ?? '')) {
        rl_fail($RL_FILE, $clientIp);
        jout(['success'=>false,'error'=>'セットアップトークンが違います（サーバーの setup_token.txt の中身を入力してください）']);
    }
    $u = trim($_POST['username'] ?? 'admin');
    $p = $_POST['password'] ?? '';
    if ($u === '' || !preg_match('/^[a-zA-Z0-9_-]{1,32}$/', $u)) jout(['success'=>false,'error'=>'IDは半角英数32文字以内にしてください']);
    if (strlen($p) < 8) jout(['success'=>false,'error'=>'パスワードは8文字以上にしてください']);
    $cfg['username'] = $u;
    $cfg['password_hash'] = password_hash($p, PASSWORD_DEFAULT);
    save_cfg($CFG_FILE, $cfg);
    @unlink($SETUP_TOKEN_FILE);  // 使い捨て
    login_ok_session();
    rl_clear($RL_FILE, $clientIp);
    jout(['success'=>true,'csrf'=>$_SESSION['csrf']]);

case 'login':
    if (($left = rl_check($RL_FILE, $clientIp)) > 0) jout(['success'=>false,'error'=>"ログイン試行が多すぎます。{$left}秒後に再試行してください"]);
    $uOk = hash_equals((string)($cfg['username'] ?? 'admin'), (string)($_POST['username'] ?? ''));
    $pOk = !$needSetup && password_verify($_POST['password'] ?? '', $cfg['password_hash']);
    if ($uOk && $pOk) {
        login_ok_session();
        rl_clear($RL_FILE, $clientIp);
        jout(['success'=>true,'csrf'=>$_SESSION['csrf']]);
    }
    rl_fail($RL_FILE, $clientIp);
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
        'live_confirmed'=>$cfg['live_confirmed'],'rules'=>[
        'symbol'=>$cfg['symbol'],'order_size'=>$cfg['order_size'],'interval'=>$cfg['interval'],
        'spread_range'=>$cfg['spread_range'],'spread_trend'=>$cfg['spread_trend'],
        'spread_threshold'=>$cfg['spread_threshold'],'crash_threshold'=>$cfg['crash_threshold'],
        'stop_loss_rate'=>$cfg['stop_loss_rate'],'max_position'=>$cfg['max_position'],
        'daily_loss_limit'=>$cfg['daily_loss_limit'],'max_order_jpy'=>$cfg['max_order_jpy'],
        'max_open_orders'=>$cfg['max_open_orders'],'post_only'=>($cfg['post_only'] === true),
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
        if (!in_array($_POST['symbol'], $SYMBOLS, true)) jout(['success'=>false,'error'=>'symbolは eth_jpy のみ対応しています']);
        $cfg['symbol'] = $_POST['symbol'];
    }
    foreach ($LIMITS as $key => [$lo, $hi]) {
        if (!isset($_POST[$key]) || $_POST[$key] === '') continue;
        if (!is_numeric($_POST[$key])) jout(['success'=>false,'error'=>"{$key} は数値で入力してください"]);
        $v = $_POST[$key] + 0;
        if ($v < $lo || $v > $hi) jout(['success'=>false,'error'=>"{$key} は {$lo} 〜 {$hi} の範囲で入力してください"]);
        $cfg[$key] = $v;
    }
    // post_only は '1'/'0' のみ受理（それ以外は拒否。文字列 "false" を true にしない）
    if (isset($_POST['post_only'])) {
        $po = $_POST['post_only'];
        if ($po !== '1' && $po !== '0') jout(['success'=>false,'error'=>'post_only は 1 か 0 で指定してください']);
        $cfg['post_only'] = ($po === '1');
    }
    save_cfg($CFG_FILE, $cfg);
    jout(['success'=>true]);

case 'confirm_live':
    // 本番モードの明示確認。APIキー未設定なら立てられない
    $en = $_POST['enable'] ?? '';
    if ($en !== '1' && $en !== '0') jout(['success'=>false,'error'=>'enable は 1 か 0 で指定してください']);
    if ($en === '1' && !keys_set($ENV_FILE)) jout(['success'=>false,'error'=>'先にAPIキーを登録してください']);
    $cfg['live_confirmed'] = ($en === '1');
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
        if (!$cfg['live_confirmed']) jout(['success'=>false,'error'=>'本番モードの明示確認がされていません。確認チェックをONにしてください']);
        if (($_POST['confirm'] ?? '') !== 'LIVE') jout(['success'=>false,'error'=>'本番起動には確認入力が必要です']);
        foreach (['max_position','daily_loss_limit','max_order_jpy','max_open_orders'] as $k) {
            if (!isset($cfg[$k]) || !is_numeric($cfg[$k])) jout(['success'=>false,'error'=>"安全設定 {$k} が未設定のため起動できません"]);
            [$lo,$hi] = $LIMITS[$k];
            if ($cfg[$k] < $lo || $cfg[$k] > $hi) jout(['success'=>false,'error'=>"安全設定 {$k} が範囲外のため起動できません"]);
        }
    }
    // 二重起動防止lock
    $lockFp = fopen($LOCK_FILE, 'c');
    if (!$lockFp || !flock($lockFp, LOCK_EX | LOCK_NB)) {
        jout(['success'=>false,'error'=>'起動/停止処理が実行中です。少し待ってから再試行してください']);
    }
    // 既存Botの停止（実体が自Botの時だけkill・終了を待つ）
    [$stopped, $stopMsg] = stop_bot_and_wait($PIDFILE);
    if (!$stopped) {
        flock($lockFp, LOCK_UN); fclose($lockFp);
        jout(['success'=>false,'error'=>'既存Botの停止に失敗: ' . $stopMsg]);
    }
    $log = $ROOT . '/bot.log';
    // venv があれば優先（依存は venv に入れる運用）
    $python = file_exists($ROOT . '/venv/bin/python3') ? $ROOT . '/venv/bin/python3' : 'python3';
    $extra = $mode === 'live' ? ' --confirm-live LIVE' : '';
    $cmd = sprintf('cd %s && nohup %s bot_v2.py --mode %s%s > %s 2>&1 & echo $!',
        escapeshellarg($ROOT), escapeshellarg($python), escapeshellarg($mode), $extra, escapeshellarg($log));
    $pid = trim(@shell_exec($cmd));
    if (!$pid || !ctype_digit($pid)) {
        flock($lockFp, LOCK_UN); fclose($lockFp);
        jout(['success'=>false,'error'=>'Botプロセスの起動に失敗しました']);
    }
    file_put_contents($PIDFILE, $pid);
    @chmod($PIDFILE, 0600);
    // 起動直後に即死していないか確認（依存不足等の失敗を success にしない）
    usleep(1500000);
    if (!pid_is_our_bot(intval($pid))) {
        @unlink($PIDFILE);
        $tail = trim((string)@shell_exec('tail -n 3 ' . escapeshellarg($log) . ' 2>/dev/null'));
        flock($lockFp, LOCK_UN); fclose($lockFp);
        jout(['success'=>false,'error'=>'Botプロセスが起動直後に停止しました: ' . ($tail ?: '原因不明')]);
    }
    flock($lockFp, LOCK_UN); fclose($lockFp);
    jout(['success'=>true,'mode'=>$mode,'pid'=>$pid]);

case 'stop':
    $lockFp = fopen($LOCK_FILE, 'c');
    if (!$lockFp || !flock($lockFp, LOCK_EX | LOCK_NB)) {
        jout(['success'=>false,'error'=>'起動/停止処理が実行中です。少し待ってから再試行してください']);
    }
    [$ok, $msg] = stop_bot_and_wait($PIDFILE);
    flock($lockFp, LOCK_UN); fclose($lockFp);
    if (!$ok) jout(['success'=>false,'error'=>$msg]);
    jout(['success'=>true,'message'=>$msg]);

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
