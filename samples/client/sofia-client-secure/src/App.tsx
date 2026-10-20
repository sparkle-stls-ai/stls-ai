import { AppDebug } from './AppDebug';
import { AppProd } from './AppProd';

const MODE = (import.meta.env.VITE_MODE as string | undefined) ?? 'debug';

export default function App() {
  return MODE === 'production' ? <AppProd /> : <AppDebug />;
}
