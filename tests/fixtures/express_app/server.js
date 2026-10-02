const express = require('express');

const app = express();
const router = express.Router();

router.get('/products/:productId', getProduct);
router.post('/products', createProduct);
router.route('/products/:productId').put(replaceProduct).delete(removeProduct);

app.use('/catalog', router);

module.exports = app;
