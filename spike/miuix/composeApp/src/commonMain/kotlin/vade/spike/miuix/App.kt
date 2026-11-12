package vade.spike.miuix

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp
import top.yukonga.miuix.kmp.basic.BasicComponent
import top.yukonga.miuix.kmp.basic.Button
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.Card
import top.yukonga.miuix.kmp.basic.NavigationBar
import top.yukonga.miuix.kmp.basic.NavigationBarItem
import top.yukonga.miuix.kmp.basic.Switch
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.basic.TopAppBar
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.theme.MiuixTheme
import top.yukonga.miuix.kmp.theme.lightColorScheme

/** The 8 components the Skin interface (C-09) will need first, one shot each. */
enum class Section(val slug: String, val title: String) {
    Button("button", "Button"),
    TextField("textfield", "TextField"),
    Card("card", "Card"),
    ListItem("listitem", "ListItem"),
    Switch("switch", "Switch"),
    BottomBar("bottombar", "BottomBar"),
    Dialog("dialog", "Dialog"),
    TopAppBar("topappbar", "TopAppBar"),
}

/**
 * A rounded-square placeholder icon. Miuix's NavigationBarItem requires an
 * ImageVector and miuix-icons is a separate artifact we do not pull in for the
 * spike; drawing one by hand keeps the dependency graph minimal.
 */
private val DemoIcon: ImageVector by lazy {
    ImageVector.Builder(
        name = "demo",
        defaultWidth = 24.dp,
        defaultHeight = 24.dp,
        viewportWidth = 24f,
        viewportHeight = 24f,
    ).apply {
        path(fill = SolidColor(Color.Black)) {
            moveTo(4f, 4f)
            lineTo(20f, 4f)
            lineTo(20f, 20f)
            lineTo(4f, 20f)
            close()
        }
    }.build()
}

@Composable
fun App(section: Section? = null) {
    MiuixTheme(colors = lightColorScheme()) {
        if (section != null) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(MiuixTheme.colorScheme.background)
                    .padding(16.dp),
            ) {
                SectionBody(section)
            }
        } else {
            FullPage()
        }
    }
}

@Composable
private fun FullPage() {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MiuixTheme.colorScheme.background),
    ) {
        TopAppBar(title = "Vade", subtitle = "Miuix on Compose Multiplatform")
        Column(
            modifier = Modifier
                .weight(1f)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(20.dp),
        ) {
            Section.entries.forEach { SectionBody(it) }
        }
        NavigationBar {
            NavigationBarItem(selected = true, onClick = {}, icon = DemoIcon, label = "聊天")
            NavigationBarItem(selected = false, onClick = {}, icon = DemoIcon, label = "会话")
            NavigationBarItem(selected = false, onClick = {}, icon = DemoIcon, label = "设置")
        }
    }
}

@Composable
private fun SectionBody(section: Section) {
    when (section) {
        Section.Button -> Buttons()
        Section.TextField -> TextFields()
        Section.Card -> Cards()
        Section.ListItem -> ListItems()
        Section.Switch -> Switches()
        Section.BottomBar -> BottomBar()
        Section.Dialog -> Dialogs()
        Section.TopAppBar -> Bars()
    }
}

@Composable
private fun Buttons() {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Button(onClick = {}, colors = ButtonDefaults.buttonColorsPrimary()) { Text("主操作") }
        Button(onClick = {}) { Text("次操作") }
        Button(
            onClick = {},
            colors = ButtonDefaults.buttonColors(
                color = Color(0xFFE5484D),
                contentColor = Color.White,
            ),
        ) { Text("删除") }
        Button(onClick = {}, enabled = false) { Text("禁用") }
        TextButton(text = "文字按钮", onClick = {})
    }
}

@Composable
private fun TextFields() {
    var name by remember { mutableStateOf("本地会话") }
    var empty by remember { mutableStateOf("") }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        TextField(
            value = name,
            onValueChange = { name = it },
            label = "会话名称",
            modifier = Modifier.fillMaxWidth(),
        )
        TextField(
            value = empty,
            onValueChange = { empty = it },
            label = "设备名（留空表示错误态对照）",
            modifier = Modifier.fillMaxWidth(),
        )
        Text("聚焦/清除按钮需真机输入法验证；此处只验证渲染。")
    }
}

@Composable
private fun Cards() {
    Card(modifier = Modifier.fillMaxWidth()) {
        Text("卡片标题")
        Text("用于验证阴影/描边与 insideMargin。Miuix 卡片默认无 Material 的 tonal elevation。")
    }
    Card(modifier = Modifier.fillMaxWidth().padding(top = 12.dp)) {
        Text("嵌套卡片外层")
        Card(modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
            Text("嵌套内层（验证圆角同心）")
        }
    }
}

@Composable
private fun ListItems() {
    Card(modifier = Modifier.fillMaxWidth()) {
        BasicComponent(
            title = "本地会话",
            summary = "seq 1024 · 已同步",
            endActions = { Text("›") },
            onClick = {},
        )
        BasicComponent(
            title = "云端设备",
            summary = "未连接",
            endActions = { Switch(checked = false, onCheckedChange = {}) },
            onClick = {},
        )
    }
}

@Composable
private fun Switches() {
    var on by remember { mutableStateOf(true) }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        BasicComponent(
            title = "深色模式",
            summary = "跟随系统",
            endActions = { Switch(checked = on, onCheckedChange = { on = it }) },
        )
        Switch(checked = !on, onCheckedChange = { on = !it })
        Text("开关动效为 Miuix 自带 spring；桌面端点击可用鼠标验证。")
    }
}

@Composable
private fun BottomBar() {
    var selected by remember { mutableStateOf(0) }
    val labels = listOf("聊天", "会话", "设置")
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        NavigationBar {
            labels.forEachIndexed { index, label ->
                NavigationBarItem(
                    selected = selected == index,
                    onClick = { selected = index },
                    icon = DemoIcon,
                    label = label,
                )
            }
        }
        Text("选中项：${labels[selected]}（点击可切换）")
    }
}

@Composable
private fun Dialogs() {
    var show by remember { mutableStateOf(true) }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Button(onClick = { show = true }) { Text("打开确认弹窗") }
        Text("对应 C-09 的 ApprovalDialog：遮罩 + 两个按钮横排。")
        OverlayDialog(
            show = show,
            title = "允许执行工具？",
            summary = "fs.write · 请求写入 3 个文件",
            onDismissRequest = { show = false },
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                TextButton(text = "拒绝", onClick = { show = false }, modifier = Modifier.weight(1f))
                TextButton(text = "允许", onClick = { show = false }, modifier = Modifier.weight(1f))
            }
        }
    }
}

@Composable
private fun Bars() {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        TopAppBar(title = "Vade", subtitle = "Miuix · Desktop")
        Text("TopAppBar 的滚动联动需要可滚动内容，见整页截图；桌面端 scrollBehavior 未接线。")
    }
}
