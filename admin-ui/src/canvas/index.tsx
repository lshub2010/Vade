/**
 * 画布模块入口。
 *
 * `React.lazy(() => import('../canvas'))` 取的是 **default 导出**，
 * 因此这里必须给 default；具名导出留给测试与同源模块直接引用。
 */

import { TopologyPage } from './TopologyPage';

export { TopologyPage };
export { FlowCanvas } from './FlowCanvas';
export { buildGraph } from './graph';

export default TopologyPage;
